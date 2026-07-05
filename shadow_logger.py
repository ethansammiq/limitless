#!/usr/bin/env python3
"""SHADOW LOGGER — dual-venue order-book depth capture for the temperature sweep.

The 2026-06/07 research left one unfinished gate on both venues: real crossable
ask + resting DEPTH at the alpha brackets during the late-day entry windows
(hourly candle volume was a proxy and last-trade price hides the spread). This
logger captures exactly that, live, with zero capital:

  Kalshi     all ~40 weather ladders (20 KXHIGH* + 20 KXLOWT*), L2 book via the
             authenticated client for every bracket whose ask is in the live
             5-95c range.
  Polymarket the US daily-high events, CLOB book per bracket (public API).

Capture is signal-agnostic on purpose: books are logged for every live-priced
bracket, and the offline join (IEM METAR archive -> running max/min bracket)
decides after the fact which quotes the sweep would have crossed. Windows are
city-LOCAL: highs 13:00-19:00 (peak forms), lows 04:00-10:00 (overnight min).

Rows carry "live": full-book rows are live=true; brackets outside the 5-95c
band get a quote-only row (live=false, summary quote, no levels) so pinned
ladders stay visible to the offline join — 2026-07-04 CHI pinned at 99c by
mid-window and simply vanished from the record. Quote-only rows are context,
not execution data (Kalshi summary quotes lag the book — the 2026-07-02
sweeper lesson). Poly runs BEFORE Kalshi: a post-wake DNS storm once burned
31 minutes of Kalshi retries and captured Poly's books half an hour stale
under a stamp from the start of the run; rows are now also timestamped at
fetch time, not run start.

Output:    logs/shadow_books/YYYY-MM-DD.jsonl   (UTC date, append-only)
Heartbeat: "shadow_logger" on every clean exit, in or out of window
           (liveness != work-done — see the 2026-06-25 bias_collector lesson).

Usage:
    python3 shadow_logger.py --once              # cron entry point
    python3 shadow_logger.py --once --force      # ignore windows (smoke test)
    python3 shadow_logger.py --once --venues poly --dry-run

Suggested crontab (NOT auto-installed):
    */30 * * * * $VENV $PROJ/shadow_logger.py --once >> /tmp/shadow_logger.log 2>&1
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

from heartbeat import write_heartbeat  # noqa: E402
from log_setup import get_logger  # noqa: E402
from market_timeseries import extract_target_date_from_ticker  # noqa: E402

logger = get_logger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent
OUT_DIR = PROJECT_ROOT / "logs" / "shadow_books"

GAMMA_URL = "https://gamma-api.polymarket.com"
CLOB_URL = "https://clob.polymarket.com"

# Live-priced band: brackets worth a book fetch. Pinned (>95c) and dead (<5c)
# quotes carry no capacity information for the sweep.
LIVE_ASK_MIN_C = 5
LIVE_ASK_MAX_C = 95
BOOK_LEVELS = 5          # top-of-book levels persisted per side
DEPTH_BAND_C = 5         # cumulative-depth band above the best ask, cents

# Entry windows, city-local hours [start, end)
HIGH_WINDOW = (13, 19)
LOW_WINDOW = (4, 10)

_TZ = {
    "ET": "America/New_York",
    "CT": "America/Chicago",
    "MT": "America/Denver",
    "AZ": "America/Phoenix",
    "PT": "America/Los_Angeles",
}

# Every Kalshi weather ladder with real volume (2026-07-01 series survey).
KALSHI_SERIES: dict[str, dict[str, str]] = {
    # highs
    "KXHIGHNY": {"tz": "ET", "kind": "high"}, "KXHIGHCHI": {"tz": "CT", "kind": "high"},
    "KXHIGHLAX": {"tz": "PT", "kind": "high"}, "KXHIGHDEN": {"tz": "MT", "kind": "high"},
    "KXHIGHMIA": {"tz": "ET", "kind": "high"}, "KXHIGHAUS": {"tz": "CT", "kind": "high"},
    "KXHIGHPHIL": {"tz": "ET", "kind": "high"}, "KXHIGHTBOS": {"tz": "ET", "kind": "high"},
    "KXHIGHTSEA": {"tz": "PT", "kind": "high"}, "KXHIGHTDC": {"tz": "ET", "kind": "high"},
    "KXHIGHTSFO": {"tz": "PT", "kind": "high"}, "KXHIGHTDAL": {"tz": "CT", "kind": "high"},
    "KXHIGHTPHX": {"tz": "AZ", "kind": "high"}, "KXHIGHTLV": {"tz": "PT", "kind": "high"},
    "KXHIGHTATL": {"tz": "ET", "kind": "high"}, "KXHIGHTHOU": {"tz": "CT", "kind": "high"},
    "KXHIGHTSATX": {"tz": "CT", "kind": "high"}, "KXHIGHTNOLA": {"tz": "CT", "kind": "high"},
    "KXHIGHTOKC": {"tz": "CT", "kind": "high"}, "KXHIGHTMIN": {"tz": "CT", "kind": "high"},
    # lows
    "KXLOWTNYC": {"tz": "ET", "kind": "low"}, "KXLOWTCHI": {"tz": "CT", "kind": "low"},
    "KXLOWTLAX": {"tz": "PT", "kind": "low"}, "KXLOWTDEN": {"tz": "MT", "kind": "low"},
    "KXLOWTMIA": {"tz": "ET", "kind": "low"}, "KXLOWTAUS": {"tz": "CT", "kind": "low"},
    "KXLOWTPHIL": {"tz": "ET", "kind": "low"}, "KXLOWTBOS": {"tz": "ET", "kind": "low"},
    "KXLOWTSEA": {"tz": "PT", "kind": "low"}, "KXLOWTDC": {"tz": "ET", "kind": "low"},
    "KXLOWTSFO": {"tz": "PT", "kind": "low"}, "KXLOWTDAL": {"tz": "CT", "kind": "low"},
    "KXLOWTPHX": {"tz": "AZ", "kind": "low"}, "KXLOWTLV": {"tz": "PT", "kind": "low"},
    "KXLOWTATL": {"tz": "ET", "kind": "low"}, "KXLOWTHOU": {"tz": "CT", "kind": "low"},
    "KXLOWTSATX": {"tz": "CT", "kind": "low"}, "KXLOWTNOLA": {"tz": "CT", "kind": "low"},
    "KXLOWTOKC": {"tz": "CT", "kind": "low"}, "KXLOWTMIN": {"tz": "CT", "kind": "low"},
}

# Polymarket daily-high events (settle on Wunderground airport stations).
# Every US city gamma listed as of 2026-07-04; one page of the date-filtered
# events query covers them all, so extra cities cost only their CLOB fetches.
POLY_CITIES: dict[str, dict[str, str]] = {
    "NYC": {"title": "Highest temperature in NYC on", "tz": "ET"},
    "CHI": {"title": "Highest temperature in Chicago on", "tz": "CT"},
    "DAL": {"title": "Highest temperature in Dallas on", "tz": "CT"},
    "SFO": {"title": "Highest temperature in San Francisco on", "tz": "PT"},
    "MIA": {"title": "Highest temperature in Miami on", "tz": "ET"},
    "ATL": {"title": "Highest temperature in Atlanta on", "tz": "ET"},
    "AUS": {"title": "Highest temperature in Austin on", "tz": "CT"},
    "HOU": {"title": "Highest temperature in Houston on", "tz": "CT"},
    "DEN": {"title": "Highest temperature in Denver on", "tz": "MT"},
    "LAX": {"title": "Highest temperature in Los Angeles on", "tz": "PT"},
    "SEA": {"title": "Highest temperature in Seattle on", "tz": "PT"},
}

_TITLE_DATE = re.compile(
    r"on (January|February|March|April|May|June|July|August|September|October"
    r"|November|December) (\d{1,2})\?")
MONTHS = {m: i + 1 for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June",
     "July", "August", "September", "October", "November", "December"])}


def in_window(kind: str, local_hour: int) -> bool:
    """Is a ladder of this kind inside its city-local capture window?"""
    lo, hi = HIGH_WINDOW if kind == "high" else LOW_WINDOW
    return lo <= local_hour < hi


def kalshi_book_metrics(book: dict) -> dict | None:
    """Depth metrics from a normalized Kalshi book ({yes/no: [[cents, qty]]}).

    Both sides are resting BIDS, so the crossable YES ask is 100 minus the best
    NO bid, and YES-taker depth is the NO-side size at/near that level.
    """
    yes_bids = sorted(book.get("yes") or [], key=lambda lv: -lv[0])
    no_bids = sorted(book.get("no") or [], key=lambda lv: -lv[0])
    if not yes_bids and not no_bids:
        return None
    best_yes_bid = yes_bids[0][0] if yes_bids else None
    yes_ask = 100 - no_bids[0][0] if no_bids else None
    ask_sz = no_bids[0][1] if no_bids else 0
    bid_sz = yes_bids[0][1] if yes_bids else 0
    cum = sum(q for c, q in no_bids if no_bids and c >= no_bids[0][0] - DEPTH_BAND_C)
    return {
        "yes_bid": best_yes_bid, "yes_ask": yes_ask,
        "bid_sz": bid_sz, "ask_sz": ask_sz, f"ask_cum{DEPTH_BAND_C}c": cum,
        "yes_levels": yes_bids[:BOOK_LEVELS], "no_levels": no_bids[:BOOK_LEVELS],
    }


def poly_book_metrics(book: dict) -> dict | None:
    """Depth metrics from a CLOB book ({bids/asks: [{price, size}]}), cents."""
    def lvls(side, reverse):
        out = []
        for lv in book.get(side) or []:
            try:
                out.append((round(float(lv["price"]) * 100, 1), float(lv["size"])))
            except (KeyError, TypeError, ValueError):
                continue
        return sorted(out, key=lambda x: -x[0] if reverse else x[0])

    bids, asks = lvls("bids", True), lvls("asks", False)
    if not bids and not asks:
        return None
    best_ask = asks[0][0] if asks else None
    cum = sum(s for p, s in asks if best_ask is not None and p <= best_ask + DEPTH_BAND_C)
    return {
        "yes_bid": bids[0][0] if bids else None, "yes_ask": best_ask,
        "bid_sz": bids[0][1] if bids else 0, "ask_sz": asks[0][1] if asks else 0,
        f"ask_cum{DEPTH_BAND_C}c": round(cum, 1),
        "bid_levels": bids[:BOOK_LEVELS], "ask_levels": asks[:BOOK_LEVELS],
    }


def is_live_priced(yes_ask_cents) -> bool:
    return yes_ask_cents is not None and LIVE_ASK_MIN_C <= yes_ask_cents <= LIVE_ASK_MAX_C


def _gamma_cents(price) -> float | None:
    """Gamma quote (0-1 dollars, number or string) -> cents, None if absent."""
    try:
        return round(float(price) * 100, 1)
    except (TypeError, ValueError):
        return None


async def capture_kalshi(now_utc: datetime, force: bool) -> list[dict]:
    import os

    from kalshi_client import KalshiClient

    targets = []
    for series, cfg in KALSHI_SERIES.items():
        local_hour = now_utc.astimezone(ZoneInfo(_TZ[cfg["tz"]])).hour
        if force or in_window(cfg["kind"], local_hour):
            targets.append(series)
    if not targets:
        return []

    client = KalshiClient(
        api_key_id=os.getenv("KALSHI_API_KEY_ID", ""),
        private_key_path=os.getenv("KALSHI_PRIVATE_KEY_PATH", ""),
        demo_mode=False,
    )
    rows: list[dict] = []
    await client.start()
    try:
        for series in targets:
            try:
                markets = await client.get_markets(series_ticker=series)
            except Exception as exc:  # noqa: BLE001 — one ladder must not kill the run
                logger.warning(f"{series}: market fetch failed: {exc}")
                continue
            for mkt in markets:
                ticker = mkt.get("ticker", "")
                base = {
                    "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "venue": "kalshi", "series": series, "ticker": ticker,
                    "target_date": extract_target_date_from_ticker(ticker),
                    "quote_bid": mkt.get("yes_bid"), "quote_ask": mkt.get("yes_ask"),
                    "vol24": mkt.get("volume_24h"), "oi": mkt.get("open_interest"),
                }
                if not is_live_priced(mkt.get("yes_ask")):
                    rows.append({**base, "live": False})
                    continue
                try:
                    metrics = kalshi_book_metrics(await client.get_orderbook(ticker))
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"{ticker}: book fetch failed: {exc}")
                    continue
                if metrics is None:
                    continue
                rows.append({**base, "live": True, **metrics})
    finally:
        await client.stop()
    return rows


def _get_json(url: str, retries: int = 3):
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "WeatherEdgeShadow/1.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except Exception as exc:  # noqa: BLE001 — network, retry
            last = exc
            time.sleep(1.0 * (attempt + 1))
    raise RuntimeError(f"fetch failed: {url[:100]} ({last})")


def capture_poly(now_utc: datetime, force: bool) -> list[dict]:
    targets = {}
    for city, cfg in POLY_CITIES.items():
        local = now_utc.astimezone(ZoneInfo(_TZ[cfg["tz"]]))
        if force or in_window("high", local.hour):
            targets[city] = (cfg, local.strftime("%Y-%m-%d"))
    if not targets:
        return []

    rows: list[dict] = []
    events: list[dict] = []
    # Date-filter server-side: the unfiltered closed=false weather list is
    # polluted with hundreds of stale never-resolved markets, so today's
    # events can drift past any fixed offset walk.
    date_min = (now_utc - timedelta(days=1)).strftime("%Y-%m-%dT00:00:00Z")
    date_max = (now_utc + timedelta(days=2)).strftime("%Y-%m-%dT00:00:00Z")
    for offset in (0, 100, 200):
        try:
            batch = _get_json(
                f"{GAMMA_URL}/events?closed=false&tag_slug=weather"
                f"&end_date_min={date_min}&end_date_max={date_max}"
                f"&limit=100&offset={offset}")
        except RuntimeError as exc:
            logger.warning(f"gamma events fetch failed: {exc}")
            break
        chunk = batch if isinstance(batch, list) else batch.get("events", [])
        if not chunk:
            break
        events.extend(chunk)

    for city, (cfg, local_day) in targets.items():
        event = None
        for ev in events:
            title = ev.get("title", "")
            m = _TITLE_DATE.search(title)
            if not title.startswith(cfg["title"]) or not m:
                continue
            if f"{MONTHS[m.group(1)]:02d}-{int(m.group(2)):02d}" == local_day[5:]:
                event = ev
                break
        if event is None:
            logger.info(f"poly {city}: no open event for {local_day}")
            continue
        for mkt in event.get("markets") or []:
            try:
                token = json.loads(mkt.get("clobTokenIds") or "[]")[0]
            except (IndexError, json.JSONDecodeError):
                continue
            base = {
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "venue": "poly", "series": f"POLY_{city}",
                "ticker": mkt.get("question", "")[:80], "token_id": token,
                "target_date": local_day,
                "vol24": round(float(mkt.get("volume24hr") or 0)),
            }
            quote_ask = _gamma_cents(mkt.get("bestAsk"))
            if not is_live_priced(quote_ask):
                rows.append({
                    **base, "live": False,
                    "yes_bid": _gamma_cents(mkt.get("bestBid")), "yes_ask": quote_ask,
                })
                continue
            try:
                metrics = poly_book_metrics(_get_json(f"{CLOB_URL}/book?token_id={token}"))
            except RuntimeError as exc:
                logger.warning(f"poly {city} book fetch failed: {exc}")
                continue
            if metrics is None:
                continue
            rows.append({**base, "live": is_live_priced(metrics["yes_ask"]), **metrics})
            time.sleep(0.1)
    return rows


def write_rows(rows: list[dict], now_utc: datetime) -> tuple[Path, bool]:
    """Append rows to the UTC-dated JSONL; True when this created the file."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"{now_utc.strftime('%Y-%m-%d')}.jsonl"
    created = not path.exists()
    with path.open("a") as fh:
        for row in rows:
            fh.write(json.dumps(row, separators=(",", ":")) + "\n")
    return path, created


def send_daily_digest(rows: list[dict], now_utc: datetime) -> None:
    """One Discord embed on the first capture of each UTC day. Never raises."""
    by_venue: dict[str, int] = {}
    for row in rows:
        by_venue[row["venue"]] = by_venue.get(row["venue"], 0) + 1
    detail = ", ".join(f"{v}: {n}" for v, n in sorted(by_venue.items()))
    try:
        from notifications import send_discord_alert

        asyncio.run(send_discord_alert(
            title="Shadow logger — first capture of the day",
            description=f"{len(rows)} book snapshots ({detail}) for {now_utc.strftime('%Y-%m-%d')} UTC.",
            color=0x3498DB,
            context="shadow_logger",
        ))
    except Exception as exc:  # noqa: BLE001 — digest must never block capture
        logger.warning(f"daily digest failed (capture unaffected): {exc}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--once", action="store_true", help="single capture (cron mode)")
    ap.add_argument("--force", action="store_true", help="ignore local-hour windows")
    ap.add_argument("--venues", default="kalshi,poly")
    ap.add_argument("--dry-run", action="store_true", help="print instead of write")
    args = ap.parse_args()
    if not args.once:
        ap.error("only --once mode is supported; schedule via cron")

    venues = {v.strip() for v in args.venues.split(",")}
    now_utc = datetime.now(timezone.utc)
    rows: list[dict] = []
    # Poly first: it is a handful of fast public fetches, while the Kalshi
    # pass can burn half an hour in post-wake DNS retries and push Poly's
    # books out of their capture window.
    if "poly" in venues:
        rows += capture_poly(now_utc, args.force)
    if "kalshi" in venues:
        rows += asyncio.run(capture_kalshi(now_utc, args.force))

    if args.dry_run:
        for row in rows:
            print(json.dumps(row))
        print(f"# {len(rows)} rows (dry run)")
    elif rows:
        path, created = write_rows(rows, now_utc)
        logger.info(f"shadow capture: {len(rows)} rows -> {path.name}")
        if created:
            send_daily_digest(rows, now_utc)
    else:
        logger.info("shadow capture: nothing in window")
    write_heartbeat("shadow_logger")


if __name__ == "__main__":
    main()
