#!/usr/bin/env python3
"""DEAD BRACKET SWEEPER — alert when observation-killed brackets still have bids.

The 2026-07-02 live find: Central Park printed 100.0°F at 17:51Z while
KXHIGHNY "98° or below" still carried ~432 contracts of YES bids
(42/38/26/22¢) — ~$108 net riskless to a YES seller. CLI settlement max is
never below a valid spot observation, so once the running max clears a
bracket's ceiling the bracket is dead; any bid on it is free money until
cancelled. Lows are the mirror image (CLI min ≤ any spot ob).

Detection, per city in config.STATIONS (verified settlement stations only —
the low series are assumed to settle on the same CLI station as the highs):
  1. NWS obs → station-local calendar-day running max and min.
  2. Lone-spike guard: the extreme must be corroborated by a second ob within
     CORROBORATION_F, else the city is skipped (bad-sensor protection).
  3. Rounding safety: back the extreme off ROUNDING_BACKOFF_F before rounding
     (METAR T-group is 0.1°C; CLI reports integer °F) so a 99.5°F ob never
     claims a certain 100° settle.
  4. Bracket bounds parse from the market SUBTITLE ("98° or below",
     "99° to 100°", "107° or above") — strike-field semantics differ between
     B- and T-tickers, subtitles don't. Unparseable subtitle → skip.
  5. Net proceeds = Σ (bid − taker_fee(bid)) × qty over YES bids ≥ MIN_BID_C.
  6. Discord alert when a run's total net ≥ --min-net, deduped via
     dead_bracket_state.json (re-alert only when a ticker's net grows 25%).

ALERT ONLY — never trades. Heartbeat "dead_bracket_sweeper" on every clean
exit, in or out of findings (liveness ≠ work-done).

Usage:
    python3 dead_bracket_sweeper.py --once            # cron entry point
    python3 dead_bracket_sweeper.py --once --dry-run  # print, no Discord/state

Suggested crontab (NOT auto-installed):
    */15 * * * * $VENV $PROJ/dead_bracket_sweeper.py --once >> /tmp/dead_bracket_sweeper.log 2>&1
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import re
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

from config import STATIONS  # noqa: E402
from dutch_book import kalshi_taker_fee_cents  # noqa: E402
from heartbeat import write_heartbeat  # noqa: E402
from log_setup import get_logger  # noqa: E402
from market_timeseries import extract_target_date_from_ticker  # noqa: E402

logger = get_logger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent
STATE_FILE = PROJECT_ROOT / "dead_bracket_state.json"
NWS_OBS_URL = "https://api.weather.gov/stations/{sid}/observations?start={start}&limit=500"

MIN_BID_C = 5              # ignore 1-4¢ dust bids
CORROBORATION_F = 5.0      # 2nd-most-extreme ob must be within this of the extreme
                           # (hourly stations gap 3-4°F on fast warm-ups — KDEN
                           # 2026-07-02; real sensor glitches run ~13°F)
ROUNDING_BACKOFF_F = 0.1   # ob precision margin before integer rounding
REALERT_GROWTH = 1.25      # re-alert a known ticker only if net grew 25%
STATE_MAX_AGE_H = 48
DEFAULT_MIN_NET_DOLLARS = 10.0

# Kalshi overnight-low ladders for the five verified stations.
LOW_SERIES = {
    "NYC": "KXLOWTNYC", "CHI": "KXLOWTCHI", "DEN": "KXLOWTDEN",
    "MIA": "KXLOWTMIA", "LAX": "KXLOWTLAX",
}

_SUB_BELOW = re.compile(r"^(-?\d+)° or below$")
_SUB_RANGE = re.compile(r"^(-?\d+)° to (-?\d+)°$")
_SUB_ABOVE = re.compile(r"^(-?\d+)° or above$")


def parse_subtitle(subtitle: str | None) -> tuple[float | None, float | None] | None:
    """Inclusive (lo, hi) bounds from a Kalshi bracket subtitle; None ends open."""
    if not subtitle:
        return None
    sub = subtitle.strip()
    if m := _SUB_BELOW.match(sub):
        return None, float(m.group(1))
    if m := _SUB_RANGE.match(sub):
        return float(m.group(1)), float(m.group(2))
    if m := _SUB_ABOVE.match(sub):
        return float(m.group(1)), None
    return None


def certain_min_settle(runmax_f: float) -> int:
    """Lowest integer the CLI max can settle at, given the observed running max."""
    return math.floor(runmax_f - ROUNDING_BACKOFF_F + 0.5)


def certain_max_settle(runmin_f: float) -> int:
    """Highest integer the CLI min can settle at, given the observed running min."""
    return math.ceil(runmin_f + ROUNDING_BACKOFF_F - 0.5)


def is_dead(kind: str, lo: float | None, hi: float | None, certain: int) -> bool:
    """Can this bracket no longer win, given the certain settle bound?"""
    if kind == "high":
        return hi is not None and hi < certain
    return lo is not None and lo > certain


def corroborated_extreme(values: list[float], kind: str) -> float | None:
    """Running max/min, or None when a lone spike could be sensor error."""
    if len(values) < 2:
        return None
    ordered = sorted(values, reverse=(kind == "high"))
    extreme, second = ordered[0], ordered[1]
    if abs(extreme - second) > CORROBORATION_F:
        return None
    return extreme


def bid_proceeds_cents(yes_bids: list, min_bid: int = MIN_BID_C) -> tuple[int, int, list]:
    """(net_cents, contracts, levels) selling YES into all bids ≥ min_bid."""
    net = contracts = 0
    levels = []
    for price, qty in yes_bids or []:
        if price < min_bid:
            continue
        net += (price - kalshi_taker_fee_cents(price)) * qty
        contracts += qty
        levels.append([price, qty])
    return net, contracts, sorted(levels, reverse=True)


def fetch_day_obs(station_id: str, tz: ZoneInfo) -> list[float]:
    """All valid temps (°F) for the station-local calendar day, via NWS API."""
    midnight_local = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    start = midnight_local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    url = NWS_OBS_URL.format(sid=station_id, start=start)
    req = urllib.request.Request(url, headers={"User-Agent": "WeatherEdgeDeadBracket/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read())
    temps = []
    for feat in payload.get("features", []):
        val = (feat.get("properties", {}).get("temperature") or {}).get("value")
        if val is not None:
            temps.append(val * 9 / 5 + 32)
    return temps


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        state = json.loads(STATE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    cutoff = datetime.now(timezone.utc) - timedelta(hours=STATE_MAX_AGE_H)
    pruned = {}
    for ticker, entry in state.items():
        try:
            if datetime.fromisoformat(entry["ts"]) >= cutoff:
                pruned[ticker] = entry
        except (KeyError, ValueError):
            continue
    return pruned


def should_alert(state: dict, ticker: str, net_cents: int) -> bool:
    prev = state.get(ticker, {}).get("net_cents")
    return prev is None or net_cents >= prev * REALERT_GROWTH


def record_alert(state: dict, ticker: str, net_cents: int) -> None:
    state[ticker] = {
        "net_cents": net_cents,
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    STATE_FILE.write_text(json.dumps(state, indent=2))


async def sweep() -> list[dict]:
    """One pass over all ladders; returns dead-bracket findings with live bids."""
    import os

    from kalshi_client import KalshiClient

    client = KalshiClient(
        api_key_id=os.getenv("KALSHI_API_KEY_ID", ""),
        private_key_path=os.getenv("KALSHI_PRIVATE_KEY_PATH", ""),
        demo_mode=False,
    )
    findings: list[dict] = []
    await client.start()
    try:
        for city, scfg in STATIONS.items():
            tz = ZoneInfo(scfg.timezone)
            local_today = datetime.now(tz).strftime("%Y-%m-%d")
            try:
                temps = fetch_day_obs(scfg.station_id, tz)
            except Exception as exc:  # noqa: BLE001 — one station must not kill the run
                logger.warning(f"{city}: obs fetch failed: {exc}")
                continue
            for kind, series in (("high", scfg.series_ticker), ("low", LOW_SERIES[city])):
                extreme = corroborated_extreme(temps, kind)
                if extreme is None:
                    logger.info(f"{city} {kind}: no corroborated extreme yet")
                    continue
                certain = certain_min_settle(extreme) if kind == "high" else certain_max_settle(extreme)
                try:
                    markets = await client.get_markets(series_ticker=series)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"{series}: market fetch failed: {exc}")
                    continue
                for mkt in markets:
                    ticker = mkt.get("ticker", "")
                    if extract_target_date_from_ticker(ticker) != local_today:
                        continue
                    bounds = parse_subtitle(mkt.get("subtitle") or mkt.get("yes_sub_title"))
                    if bounds is None or not is_dead(kind, *bounds, certain):
                        continue
                    # No yes_bid pre-filter: the summary quote lags the book
                    # (observed 2026-07-02 — summary read 0 while the book
                    # still held 432 contracts). Dead brackets are rare, so
                    # always read the book.
                    try:
                        book = await client.get_orderbook(ticker)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(f"{ticker}: book fetch failed: {exc}")
                        continue
                    net, contracts, levels = bid_proceeds_cents((book or {}).get("yes"))
                    if net <= 0:
                        continue
                    findings.append({
                        "ticker": ticker,
                        "subtitle": mkt.get("subtitle") or mkt.get("yes_sub_title"),
                        "kind": kind, "city": city, "station": scfg.station_id,
                        "extreme_f": round(extreme, 1), "certain_settle": certain,
                        "net_cents": net, "contracts": contracts, "levels": levels,
                    })
    finally:
        await client.stop()
    return findings


def format_alert(findings: list[dict]) -> tuple[str, str]:
    total = sum(f["net_cents"] for f in findings)
    title = f"💰 DEAD BRACKET — {len(findings)} bracket(s), ~${total / 100:.2f} net riskless"
    lines = []
    for f in findings:
        word = "runmax" if f["kind"] == "high" else "runmin"
        bound = "≥" if f["kind"] == "high" else "≤"
        levels = ", ".join(f"{p}¢×{q}" for p, q in f["levels"])
        lines.append(
            f"**{f['ticker']}** ({f['subtitle']}) — {f['station']} {word} "
            f"{f['extreme_f']}°F → settles {bound}{f['certain_settle']}°\n"
            f"  bids: {levels} → sell YES, net ~${f['net_cents'] / 100:.2f} "
            f"({f['contracts']} contracts)"
        )
    lines.append("_Alert only — verify the obs trail before trading._")
    return title, "\n".join(lines)


def send_alert(findings: list[dict]) -> None:
    """Discord alert; failure must never block the heartbeat."""
    title, description = format_alert(findings)
    try:
        from notifications import send_discord_alert

        asyncio.run(send_discord_alert(
            title=title, description=description[:4096],
            color=0x2ECC71, context="dead_bracket_sweeper",
        ))
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"discord alert failed: {exc}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--once", action="store_true", help="single sweep (cron mode)")
    ap.add_argument("--dry-run", action="store_true", help="print, skip Discord/state")
    ap.add_argument("--min-net", type=float, default=DEFAULT_MIN_NET_DOLLARS,
                    help="alert only when a run's total net proceeds ≥ this ($)")
    args = ap.parse_args()
    if not args.once:
        ap.error("only --once mode is supported; schedule via cron")

    findings = asyncio.run(sweep())
    total = sum(f["net_cents"] for f in findings)

    if not findings:
        logger.info("dead-bracket sweep: nothing found")
    elif args.dry_run:
        title, description = format_alert(findings)
        print(title)
        print(description)
    elif total >= args.min_net * 100:
        state = load_state()
        fresh = [f for f in findings if should_alert(state, f["ticker"], f["net_cents"])]
        if fresh:
            send_alert(fresh)
            for f in fresh:
                record_alert(state, f["ticker"], f["net_cents"])
            logger.info(f"dead-bracket sweep: alerted {len(fresh)} (${total / 100:.2f})")
        else:
            logger.info("dead-bracket sweep: findings already alerted")
    else:
        logger.info(f"dead-bracket sweep: ${total / 100:.2f} below ${args.min_net:.2f} floor")
    write_heartbeat("dead_bracket_sweeper")


if __name__ == "__main__":
    main()
