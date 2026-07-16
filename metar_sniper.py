#!/usr/bin/env python3
"""METAR SNIPER — race the 6-hourly climate groups, hours before the CLI.

2026-07-11, live: KMSP 112353Z carried remark `10322` (6-hr max 32.2°C =
89.96°F → the CLI printed 90) and the 99¢×119k certainty wall repriced
KXHIGHTMIN-26JUL11-B88.5 immediately after that METAR. The walls read
these groups; this job reads them in the same minute and alerts when the
implied winning bracket differs from what the book still prices — the
same race-the-repricing edge class as the CLI sniper, one leak earlier.

Semantics (core.metar):
  6-hr max group (1sTTT) ⇒ FLOOR on the day's high — brackets with
      hi < value are dead; the bracket containing it leads (warming risk
      remains until the day ends).
  6-hr min group (2sTTT) ⇒ CEILING on the day's low — brackets with
      lo > value are dead; the containing bracket leads but the min can
      still fall until midnight LST (same open forecast bet as the CLI
      low floor: journaled, never alerted as a buy).

Per run (cron */5): if now-UTC is inside a synoptic fetch window (the
:53 obs before 00/06/12/18Z carry the groups), ONE aviationweather.gov
request covers every ladder station; UNSEEN (station, obs-time, kind)
extremes classify their ladders' books; opportunities → Discord with a
ready take.py command. The not-yet-repriced test IS the ask cap: a book
that already moved trades above MAX_BUY_ASK_C and never alerts.

ALERT ONLY — never trades. Journal: logs/metar_sniper/YYYY-MM-DD.jsonl.
Heartbeat "metar_sniper" on every clean exit.

Usage:
    python3 metar_sniper.py --once                 # cron entry point
    python3 metar_sniper.py --once --dry-run       # print, no Discord/state
    python3 metar_sniper.py --replay KMSP          # latest groups for one
                                                   # station, any hour, no state

Suggested crontab (NOT auto-installed):
    */5 * * * * $VENV $PROJ/metar_sniper.py --once >> /tmp/metar_sniper.log 2>&1
"""
from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

from core import metar  # noqa: E402
from core.brackets import contains, is_dead, parse_subtitle  # noqa: E402
from core.fees import kalshi_taker_fee_cents  # noqa: E402
from core.io import atomic_write_json  # noqa: E402
from core.obs import (  # noqa: E402
    annotate_floor_buys, corroborated_extreme, fetch_day_obs_timed, trend_class)
from core.risk import MAX_ENTRY_ASK_C  # noqa: E402
from core.walls import WALL_ASK_DEPTH  # noqa: E402
from dead_bracket_sweeper import bid_proceeds_cents  # noqa: E402
from heartbeat import write_heartbeat  # noqa: E402
from ladders import Ladder, by_station  # noqa: E402
from log_setup import get_logger  # noqa: E402
from market_timeseries import extract_target_date_from_ticker  # noqa: E402

logger = get_logger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent
STATE_FILE = PROJECT_ROOT / "metar_sniper_state.json"
JOURNAL_DIR = PROJECT_ROOT / "logs" / "metar_sniper"

# The groups ride the :53 obs before each synoptic hour (00/06/12/18Z),
# i.e. ~2353Z/0553Z/1153Z/1753Z, with stragglers and corrections trailing.
# Fetch from 10 min before the :53 ob until 45 min past the synoptic hour.
SYNOPTIC_HOURS_UTC = (0, 6, 12, 18)
WINDOW_BEFORE_MIN = 17    # from HH-1:43 (10 min before the :53 ob)
WINDOW_AFTER_MIN = 45     # until HH:45

SEEN_MAX_AGE_H = 36
ALERTED_MAX_AGE_H = 48
MAX_BUY_ASK_C = MAX_ENTRY_ASK_C  # the standing entry cap (core/risk.py) —
                                 # doubles as the repriced-already filter
MIN_SELL_NET_C = 100      # dead-bid alert floor, cents ($1)


def in_fetch_window(now_utc: datetime) -> bool:
    """Is now inside a synoptic-group publication window (UTC-fixed)?"""
    for hh in SYNOPTIC_HOURS_UTC:
        anchor = now_utc.replace(hour=hh, minute=0, second=0, microsecond=0)
        for day_shift in (0, 1):  # 2343Z belongs to the NEXT day's 00Z anchor
            a = anchor + timedelta(days=day_shift)
            if (a - timedelta(minutes=WINDOW_BEFORE_MIN) <= now_utc
                    <= a + timedelta(minutes=WINDOW_AFTER_MIN)):
                return True
    return False


def classify(extreme: metar.SixHrExtreme, ladder: Ladder,
             markets: list[dict]) -> list[dict]:
    """Decisions one 6-hourly extreme forces on one ladder's brackets.

    High ladders consume max groups (floor), low ladders min groups
    (ceiling) — a mismatched kind classifies nothing. °F comes from the
    precise tenths value (89.96 → 90), and the same is_dead/contains
    semantics as the CLI floor apply: is_dead(kind=high) kills hi < value,
    is_dead(kind=low) kills lo > value.
    """
    if (ladder.kind == "high") != (extreme.kind == "max"):
        return []
    target_date = metar.climate_date(extreme, ladder.tz)
    if target_date is None:
        return []
    value = extreme.temp_f_rounded
    findings = []
    for mkt in markets or []:
        ticker = mkt.get("ticker", "")
        if extract_target_date_from_ticker(ticker) != target_date:
            continue
        subtitle = mkt.get("subtitle") or mkt.get("yes_sub_title")
        bounds = parse_subtitle(subtitle)
        if bounds is None:
            continue
        base = {"ticker": ticker, "subtitle": subtitle,
                "series": ladder.series, "ladder_kind": ladder.kind,
                "printed": value, "precise_c": extreme.temp_c,
                "precise_f": round(extreme.temp_f, 2),
                "obs_time": extreme.obs_time_utc.isoformat(timespec="minutes"),
                "synoptic_anchor_utc": metar.synoptic_anchor_utc(
                    extreme.obs_time_utc),
                "final": False}
        if is_dead(ladder.kind, bounds[0], bounds[1], value):
            findings.append({**base, "kind": "sell_dead"})
        elif contains(bounds[0], bounds[1], value):
            findings.append({**base, "kind": "buy_winner"})
    return findings


def cli_floor_crosscheck(extreme: metar.SixHrExtreme, awips: str, tz: str,
                         cli_entries: list[dict]) -> list[dict]:
    """Confirm or bust journaled CLI floor buys against a new 6-hr max group.

    The archive study (backtest/metar_leak_study.py, 828 station-days):
    the day-max of 6-hr groups == final CLI 98.4%, and on floor≠final
    drift days it named the final 50/52 — ~8h before the final CLI. The
    live scorecard killed this feed as an ENTRY generator (PM buys 0/12:
    anything still cheap after the walls read the METAR is adversely
    selected), so its value flows to positions ALREADY taken off the CLI
    floor print:

      cli_bust    — the group exceeds the bought bracket's hi: the final
                    follows the METAR out of the bracket (floor semantics,
                    certain) → exit while bids last.
      cli_confirm — the 00Z-anchor group (the only anchor with the full
                    day's group set) lands inside the bracket → the ~86%
                    floor-at-top drift risk collapses to the archive's
                    ~98% class.

    Checks are incremental — every new max group can bust; only the 00Z
    anchor confirms — so an 18Z group that already left the bracket fires
    the bust hours before the confirm window. Station-days whose final CLI
    is already journaled are skipped (the document itself has resolved
    drift; live_watch owns settled positions).
    """
    if extreme.kind != "max":
        return []
    target_date = metar.climate_date(extreme, tz)
    if target_date is None:
        return []
    # skipped guard: DEN's pre-dawn same-day dailies journaled is_final=true
    # (raw regex label) until 2026-07-16, muting busts/confirms all day —
    # rows already on disk keep the flag, so filter here too.
    finals = {(e.get("awips"), e.get("summary_date"))
              for e in cli_entries if e.get("is_final") and not e.get("skipped")}
    if (awips, target_date) in finals:
        return []
    v = extreme.temp_f_rounded
    anchor = metar.synoptic_anchor_utc(extreme.obs_time_utc)
    out, seen_tickers = [], set()
    for e in cli_entries:
        if (e.get("awips") != awips or e.get("summary_date") != target_date
                or e.get("is_final")):
            continue
        for f in e.get("findings") or []:
            if (f.get("kind") != "buy_winner"
                    or f.get("ladder_kind") != "high"
                    or f.get("suppressed")
                    or f.get("ticker") in seen_tickers):
                continue
            bounds = parse_subtitle(f.get("subtitle"))
            if bounds is None:
                continue
            seen_tickers.add(f["ticker"])
            base = {"ticker": f["ticker"], "subtitle": f.get("subtitle"),
                    "series": f.get("series"), "ladder_kind": "high",
                    "cli_floor": f.get("printed"), "printed": v,
                    "precise_c": extreme.temp_c,
                    "precise_f": round(extreme.temp_f, 2),
                    "obs_time": extreme.obs_time_utc.isoformat(timespec="minutes"),
                    "synoptic_anchor_utc": anchor, "final": False}
            if bounds[1] is not None and v > bounds[1]:
                out.append({**base, "kind": "cli_bust"})
            elif anchor == 0 and contains(bounds[0], bounds[1], v):
                out.append({**base, "kind": "cli_confirm"})
    return out


def _alert_key(o: dict) -> str:
    """Dedup key: crosscheck kinds are per-(kind, ticker) — a bust must not
    be swallowed because the same ticker confirmed (or alerted) earlier."""
    if o["kind"] in ("cli_bust", "cli_confirm"):
        return f"{o['kind']}:{o['ticker']}"
    return o["ticker"]


def _take_cmd(action: str, ticker: str, qty: int, price_c: int) -> str:
    return (f".venv/bin/python scripts/take.py {ticker} {action} yes "
            f"{qty} {price_c}")


async def _price_findings(client, findings: list[dict]) -> list[dict]:
    """Attach live book economics; keep only actionable findings.

    A buy only alerts under MAX_BUY_ASK_C — a book already repriced to the
    METAR trades above it, and skipping that is the point (no repricing
    left to race). Low-ladder buy_winners are journal-only: a 6-hr min is
    a ceiling, not a lock (the CLI low-floor class measured -30.8¢).
    """
    priced = []
    for f in findings:
        try:
            book = await client.get_orderbook(f["ticker"])
        except Exception as exc:  # noqa: BLE001 — one book must not kill the run
            logger.warning(f"{f['ticker']}: book fetch failed: {exc}")
            continue
        yes_bids = sorted((book or {}).get("yes") or [], key=lambda lv: -lv[0])
        no_bids = sorted((book or {}).get("no") or [], key=lambda lv: -lv[0])
        if f["kind"] == "sell_dead":
            net, contracts, levels = bid_proceeds_cents(yes_bids)
            if net >= MIN_SELL_NET_C:
                price = levels[-1][0] if levels else 0
                priced.append({**f, "net_cents": net, "contracts": contracts,
                               "levels": levels,
                               "cmd": _take_cmd("sell", f["ticker"], contracts, price)})
        else:  # buy_winner
            if not no_bids:
                continue
            ask = 100 - no_bids[0][0]
            depth = no_bids[0][1]
            if 1 <= ask <= MAX_BUY_ASK_C and depth >= 1:
                qty = max(1, int(depth))
                entry = {**f, "ask": ask, "ask_depth": depth,
                         "fee_c": kalshi_taker_fee_cents(ask),
                         "cmd": _take_cmd("buy", f["ticker"], qty, ask)}
                if depth >= WALL_ASK_DEPTH:
                    entry["wall_ask"] = True
                if f["ladder_kind"] == "low":
                    entry.pop("cmd")
                    entry["suppressed"] = "low_ceiling_forecast"
                priced.append(entry)
    return priced


def format_alert(opps: list[dict]) -> tuple[str, str]:
    n_buy = sum(1 for o in opps if o["kind"] == "buy_winner")
    n_sell = sum(1 for o in opps if o["kind"] == "sell_dead")
    title = f"📡 METAR 6-HR SNIPER — {n_buy} winner buy(s), {n_sell} dead-bid sell(s)"
    n_bust = sum(1 for o in opps if o["kind"] == "cli_bust")
    n_conf = sum(1 for o in opps if o["kind"] == "cli_confirm")
    if n_bust or n_conf:
        title += f", {n_bust} CLI bust(s), {n_conf} CLI confirm(s)"
    lines = []
    for o in opps:
        provenance = (f"6-hr {'max' if o['ladder_kind'] == 'high' else 'min'} "
                      f"{o['precise_c']}°C = {o['precise_f']}°F → **{o['printed']}°** "
                      f"(ob {o['obs_time']})")
        if o["kind"] == "cli_bust":
            lines.append(
                f"⛔ **{o['ticker']}** ({o['subtitle']}) — CLI floor buy "
                f"BUSTED: floor was {o['cli_floor']}° but {provenance} — the "
                f"final follows the METAR out of this bracket (archive "
                f"50/52 on drift days). Exit bids while they last.")
            continue
        if o["kind"] == "cli_confirm":
            lines.append(
                f"✅ **{o['ticker']}** ({o['subtitle']}) — CLI floor buy "
                f"CONFIRMED: 00Z day-max group {provenance} lands in-bracket "
                f"→ drift risk collapses to the ~98% archive class "
                f"(day-max == final 815/828). Hold to settlement.")
            continue
        if o["kind"] == "buy_winner":
            risk = ("post-window warming risk" if o["ladder_kind"] == "high"
                    else "min can still fall")
            warn = ""
            if o.get("obs_kill"):
                warn = f"\n  🚫 **{o['obs_kill']}** — do not buy"
            elif o.get("obs_warn"):
                warn = f"\n  ⚠️ **{o['obs_warn']}**"
            elif "obs_max_f" in o:
                warn = f" | obs so far {o['obs_max_f']}°"
            if o.get("wall_ask"):
                warn += (f"\n  🧱 {o['ask_depth']:.0f}-deep ask wall — walls are "
                         f"5-0 vs floor signals, never fade")
            lines.append(
                f"**{o['ticker']}** ({o['subtitle']}) — {provenance} "
                f"[floor, {risk}] → ask {o['ask']}¢ × {o['ask_depth']:.0f}, "
                f"fee {o['fee_c']}¢{warn}\n  `{o['cmd']}`")
        else:
            levels = ", ".join(f"{p}¢×{q}" for p, q in o["levels"])
            lines.append(
                f"**{o['ticker']}** ({o['subtitle']}) — dead vs {provenance} "
                f"→ bids {levels}, net ~${o['net_cents'] / 100:.2f}\n  `{o['cmd']}`")
    lines.append("_Alert only — hours ahead of the CLI; the raw METAR is in "
                 "the journal; verify before trading._")
    return title, "\n".join(lines)


def _seen_key(e: metar.SixHrExtreme) -> str:
    return f"{e.station}:{e.obs_time_utc.strftime('%d%H%M')}:{e.kind}"


def _load_state() -> dict:
    state = {"seen": {}, "alerted": {}}
    if STATE_FILE.exists():
        try:
            state.update(json.loads(STATE_FILE.read_text()))
        except (json.JSONDecodeError, OSError):
            pass
    now = datetime.now(timezone.utc)
    cutoff_seen = (now - timedelta(hours=SEEN_MAX_AGE_H)).isoformat()
    cutoff_alerted = (now - timedelta(hours=ALERTED_MAX_AGE_H)).isoformat()
    state["seen"] = {k: v for k, v in state["seen"].items() if v >= cutoff_seen}
    state["alerted"] = {k: v for k, v in state["alerted"].items()
                        if v.get("ts", "") >= cutoff_alerted}
    return state


def _journal(entry: dict, now_utc: datetime) -> None:
    JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    path = JOURNAL_DIR / f"{now_utc.strftime('%Y-%m-%d')}.jsonl"
    with path.open("a") as fh:
        fh.write(json.dumps(entry, separators=(",", ":")) + "\n")


async def run(dry_run: bool, replay: str | None) -> None:
    import os

    from kalshi_client import KalshiClient

    now_utc = datetime.now(timezone.utc)
    groups = by_station()
    state = _load_state()

    if replay:
        icao = replay.upper()
        if icao not in groups:
            raise SystemExit(f"unknown station {icao!r} — not in ladders.json")
        stations = [icao]
    else:
        if not in_fetch_window(now_utc):
            logger.info("metar sniper: outside synoptic windows")
            return
        stations = sorted(groups)

    try:
        raw = metar.fetch_metars(stations, hours=8 if replay else 3)
    except Exception as exc:  # noqa: BLE001 — fail open, retry next cron
        logger.warning(f"metar fetch failed: {exc}")
        return

    extremes = metar.parse_metars(raw, now_utc)
    if not replay:
        extremes = [e for e in extremes if _seen_key(e) not in state["seen"]]
    extremes = [e for e in extremes if e.station in groups]
    if not extremes:
        if not dry_run and not replay:
            atomic_write_json(STATE_FILE, state, indent=1)
        logger.info("metar sniper: no new 6-hourly groups")
        return

    client = KalshiClient(
        api_key_id=os.getenv("KALSHI_API_KEY_ID", ""),
        private_key_path=os.getenv("KALSHI_PRIVATE_KEY_PATH", ""),
        demo_mode=False,
    )
    opportunities: list[dict] = []
    # CLI journal read once per run (VPS-local; empty on the dev Mac).
    from cli_sniper import _recent_journal_entries

    cli_entries = _recent_journal_entries(now_utc)
    await client.start()
    try:
        obs_cache: dict[str, tuple[float | None, float | None, dict | None]] = {}
        for extreme in extremes:
            key = _seen_key(extreme)
            entry = {"ts": now_utc.isoformat(timespec="seconds"),
                     "station": extreme.station, "kind": extreme.kind,
                     "obs_time": extreme.obs_time_utc.isoformat(timespec="minutes"),
                     "tenths_c": extreme.tenths_c,
                     "temp_f": round(extreme.temp_f, 2),
                     "rounded_f": extreme.temp_f_rounded,
                     "findings": []}
            read_ok = True
            for ladder in groups[extreme.station]:
                if (ladder.kind == "high") != (extreme.kind == "max"):
                    continue
                if metar.climate_date(extreme, ladder.tz) is None:
                    entry["skipped"] = "window_straddles_midnight"
                    continue
                markets, ok = await client.get_markets_checked(
                    series_ticker=ladder.series)
                if not ok:
                    read_ok = False
                    logger.warning(f"{ladder.series}: market read degraded — "
                                   f"{extreme.station} left unseen for retry")
                    continue
                findings = classify(extreme, ladder, markets)
                priced = await _price_findings(client, findings)
                # Obs-vs-floor stamps (shared with cli_sniper): a 6-hr max
                # is a FLOOR with the same post-window warming risk — the
                # 2026-07-13 morning batch staged T-threshold buttons the
                # day then warmed straight through.
                if (extreme.station not in obs_cache
                        and any(p.get("kind") == "buy_winner"
                                and p.get("ladder_kind") == "high"
                                and not p.get("suppressed") for p in priced)):
                    try:
                        timed = await asyncio.to_thread(
                            fetch_day_obs_timed, extreme.station,
                            ZoneInfo(ladder.tz))
                        temps = [f for _, f in timed]
                        obs_cache[extreme.station] = (
                            corroborated_extreme(temps, "high"),
                            max(temps) if temps else None,
                            trend_class(timed, now_utc))
                    except Exception as exc:  # noqa: BLE001 — fail-open
                        logger.warning(f"{extreme.station}: obs fetch failed: {exc}")
                        obs_cache[extreme.station] = (None, None, None)
                cmax, rmax, trend = obs_cache.get(extreme.station,
                                                  (None, None, None))
                annotate_floor_buys(priced, cmax, rmax, trend=trend)
                entry["findings"] += [
                    {k: v for k, v in f.items() if k != "cmd"} for f in priced]
                opportunities += [p for p in priced if not p.get("suppressed")]
            # Confirm/bust journaled CLI floor buys against this group —
            # no market read needed; the METAR is the information.
            crosschecks = cli_floor_crosscheck(
                extreme, groups[extreme.station][0].awips,
                groups[extreme.station][0].tz, cli_entries)
            for c in crosschecks:
                logger.warning(f"{c['ticker']}: {c['kind'].upper()} — CLI "
                               f"floor {c['cli_floor']}° vs 6-hr max "
                               f"{c['printed']}°")
            entry["findings"] += crosschecks
            opportunities += crosschecks
            # Mark seen only on a clean sweep — a degraded read leaves the
            # extreme for the next cron to retry (fail closed on state).
            if read_ok and not replay:
                state["seen"][key] = now_utc.isoformat(timespec="seconds")
            if not dry_run:
                _journal(entry, now_utc)
            logger.info(f"{extreme.station}: 6-hr {extreme.kind} "
                        f"{extreme.temp_c}°C → {extreme.temp_f_rounded}°F, "
                        f"{len(entry['findings'])} finding(s)")
    finally:
        await client.stop()

    fresh = [o for o in opportunities
             if replay or _alert_key(o) not in state["alerted"]]
    if dry_run or replay:
        if fresh:
            title, body = format_alert(fresh)
            print(title)
            print(body)
        else:
            print("no actionable opportunities")
        return

    if fresh:
        title, body = format_alert(fresh)
        # Phone-push (@mention) only when something is actually takeable —
        # trap-stamped or suppressed findings stay silent embeds. cli_bust
        # pings too: it's an exit-now signal on an open position.
        actionable = any(
            o["kind"] == "cli_bust"
            or (o["kind"] in ("buy_winner", "sell_dead")
                and not (o.get("suppressed") or o.get("obs_kill")
                         or o.get("obs_warn") or o.get("wall_ask")))
            for o in fresh)
        try:
            from notifications import send_discord_alert

            await send_discord_alert(title=title, description=body[:4096],
                                     color=0x3498DB, context="metar_sniper",
                                     mention=actionable)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"discord alert failed: {exc}")
        try:
            from core import take_queue

            staged = take_queue.enqueue_findings(fresh, source="metar_sniper",
                                                 now_utc=now_utc)
            if staged:
                logger.info(f"take queue: staged {staged} command(s) "
                            f"for one-tap approve")
                # Post the button NOW — the floor race reprices inside the
                # 0-60s wait for the approver's next tick (2026-07-14 ×3).
                from take_approver import post_new_entries

                await post_new_entries()
        except Exception as exc:  # noqa: BLE001 — staging must not break alerting
            logger.warning(f"take queue enqueue failed: {exc}")
        for o in fresh:
            state["alerted"][_alert_key(o)] = {
                "ts": now_utc.isoformat(timespec="seconds"),
                "printed": o["printed"]}
        logger.info(f"metar sniper: alerted {len(fresh)} opportunity(ies)")
    atomic_write_json(STATE_FILE, state, indent=1)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--once", action="store_true", help="single pass (cron mode)")
    ap.add_argument("--dry-run", action="store_true", help="print, no Discord/state")
    ap.add_argument("--replay", metavar="ICAO",
                    help="run the pipeline on one station's recent METARs, any hour")
    args = ap.parse_args()
    if not args.once and not args.replay:
        ap.error("use --once (cron) or --replay ICAO")
    # Single-instance run lock (same overlap class as cli_sniper): locked-out
    # runs exit WITHOUT heartbeating so a hung instance trips the watchdog.
    import fcntl
    with (PROJECT_ROOT / ".metar_sniper.lock").open("w") as lock_fd:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            logger.info("metar sniper: previous run still active — skipping")
            return
        asyncio.run(run(args.dry_run, args.replay))
        write_heartbeat("metar_sniper")


if __name__ == "__main__":
    main()
