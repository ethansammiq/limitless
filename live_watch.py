#!/usr/bin/env python3
"""LIVE WATCH — read-only journal + sell-into-strength alerts for the LIVE account.

The bot's guards, exits, and ledger all live on the paper path; real-money
trades enter manually and were invisible to everything (2026-07-04: the first
live positions existed only in Kalshi's database). This job closes that gap
WITHOUT trading:

  fills     new fills (deduped by fill_id) -> logs/live_fills.jsonl
  positions snapshot of nonzero positions  -> logs/live_positions.jsonl
  balance   appended on change             -> logs/live_balance.jsonl (equity curve)
  alert     long YES position with best bid >= --threshold (default 85c)
            -> Discord "sell into strength" ping, deduped via
            live_watch_state.json (re-pings only if the bid climbs 3c+)
  boundary  low-ladder brackets whose floor the evening obs are walking
            toward before midnight LST, with real bids still on them
            -> mention ping (position-free; the BOS-26JUL16 gap)

READ-ONLY by design: it tells you when to sell; it never sells. (Live order
placement is mid-migration to Kalshi's V2 schema anyway.)

Usage:
    python3 live_watch.py --once              # cron entry point
    python3 live_watch.py --once --dry-run    # print, no writes/Discord

Suggested crontab (NOT auto-installed):
    */10 * * * * $VENV $PROJ/live_watch.py --once >> /tmp/live_watch.log 2>&1
"""
from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

from core.io import atomic_write_json  # noqa: E402
from heartbeat import write_heartbeat  # noqa: E402
from kalshi_client import parse_dollars  # noqa: E402
from log_setup import get_logger  # noqa: E402
from market_timeseries import extract_target_date_from_ticker  # noqa: E402

logger = get_logger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent
LOGS = PROJECT_ROOT / "logs"
FILLS_LOG = LOGS / "live_fills.jsonl"
POSITIONS_LOG = LOGS / "live_positions.jsonl"
BALANCE_LOG = LOGS / "live_balance.jsonl"
# Latest full-account snapshot (overwritten each run) — the dashboard reads
# this to show REAL money instead of the paper ledger.
ACCOUNT_FILE = LOGS / "live_account.json"
STATE_FILE = PROJECT_ROOT / "live_watch_state.json"

DEFAULT_STRENGTH_C = 85
REALERT_STEP_C = 3
# A 1-lot 99c flicker is a phantom quote, not an exit (2026-07-04: a bid
# printed 99c for seconds on a book whose last trade was 18c). Only alert
# when there's real size within a few cents of the best bid.
MIN_ALERT_DEPTH = 5
DEPTH_BAND_C = 3
# Kalshi normally settles a weather ladder within hours of the final CLI, but
# KXLOWTMIA-26JUL07 sat undetermined for 50+ hours (2026-07-10) with $35 of
# collateral locked, noticed only by a human happening to look. Any position
# whose event date is this many days past gets one overdue ping per day.
SETTLEMENT_OVERDUE_DAYS = 2

# ── Low-ladder boundary watch (2026-07-16) ──────────────────────────────
# Built after KXLOWTBOS-26JUL16-T68: the evening cold push walked KBOS
# toward the leading bracket's 69° floor with the market still bidding it
# 83¢ — and nothing pinged, because live_watch only watched POSITIONS and
# the sweeper only speaks after a bracket is corroborated-dead. This is
# the pre-cross heads-up: in the last hours before midnight LST (when a
# low ladder's min locks), mention-alert any alive bracket whose floor
# the latest obs are approaching while real money still sits on it.
# Info-only — never staged, never a take command; on an actual cross the
# sweeper's sell_dead path takes over.
BOUNDARY_WATCH_HOURS = 4    # engage this close to midnight LST
BOUNDARY_APPROACH_F = 2.0   # alert once the latest ob is this near a floor
BOUNDARY_REALERT_F = 1.0    # re-ping per this much closer
BOUNDARY_MIN_BID_C = 15     # below this YES bid there's no money at stake


def overdue_settlements(positions: list[dict], today_utc) -> list[tuple[dict, int]]:
    """(position, age_days) for nonzero positions whose event date is
    >= SETTLEMENT_OVERDUE_DAYS in the past — still unsettled long after the
    settlement document existed."""
    out = []
    for p in positions or []:
        try:
            qty = float(p.get("position_fp") or p.get("position") or 0)
        except (TypeError, ValueError):
            qty = 0.0
        if qty == 0:
            continue
        tgt = extract_target_date_from_ticker(p.get("ticker", ""))
        if not tgt:
            continue
        try:
            event_day = datetime.strptime(tgt, "%Y-%m-%d").date()
        except ValueError:
            continue
        age = (today_utc - event_day).days
        if age >= SETTLEMENT_OVERDUE_DAYS:
            out.append((p, age))
    return out


def should_alert_overdue(state: dict, ticker: str, today_iso: str) -> bool:
    """One overdue ping per ticker per day."""
    return state.get(f"overdue:{ticker}") != today_iso


def _load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def known_fill_ids() -> set[str]:
    if not FILLS_LOG.exists():
        return set()
    ids = set()
    for line in FILLS_LOG.read_text().splitlines():
        try:
            fid = json.loads(line).get("fill_id")
        except json.JSONDecodeError:
            continue
        if fid:
            ids.add(fid)
    return ids


def new_fills(fills: list[dict], known: set[str]) -> list[dict]:
    return [f for f in fills or [] if f.get("fill_id") and f["fill_id"] not in known]


def last_logged_balance() -> float | None:
    if not BALANCE_LOG.exists():
        return None
    lines = BALANCE_LOG.read_text().splitlines()
    for line in reversed(lines):
        try:
            return json.loads(line).get("balance")
        except json.JSONDecodeError:
            continue
    return None


def open_long_positions(positions: list[dict]) -> list[dict]:
    out = []
    for p in positions or []:
        try:
            qty = float(p.get("position_fp") or 0)
        except (TypeError, ValueError):
            continue
        if qty > 0:
            out.append({**p, "qty": qty})
    return out


def bid_depth_near_best(yes_bids_sorted: list, band: int = DEPTH_BAND_C) -> float:
    """Contracts resting within `band` cents of the best YES bid."""
    if not yes_bids_sorted:
        return 0
    best = yes_bids_sorted[0][0]
    return sum(q for p, q in yes_bids_sorted if p >= best - band)


def should_alert_strength(state: dict, ticker: str, bid: int, threshold: int,
                          depth: float = MIN_ALERT_DEPTH) -> bool:
    if bid < threshold or depth < MIN_ALERT_DEPTH:
        return False
    prev = state.get(ticker, {}).get("bid")
    return prev is None or bid >= prev + REALERT_STEP_C


def boundary_candidates(markets: list[dict], obs: list[tuple[datetime, float]],
                        now_utc: datetime, day_end_utc: datetime) -> list[dict]:
    """Alive low-ladder brackets whose floor the latest ob is approaching.

    Pure temperature math (books are the caller's cut): a candidate has a
    lower bound, is not corroborated-dead (that's the sweeper's territory),
    and the latest precise ob sits within BOUNDARY_APPROACH_F of its floor
    with time left before midnight LST.
    """
    from core.brackets import parse_subtitle
    from core.obs import certain_max_settle, corroborated_extreme

    minutes_left = (day_end_utc - now_utc).total_seconds() / 60
    day_obs = [(t, f) for t, f in obs if t <= now_utc]
    if not day_obs or minutes_left <= 0:
        return []
    run_min = min(f for _, f in day_obs)
    ob_time, latest_f = day_obs[-1]
    # Dead-skip only on a CORROBORATED min — a lone down-spike must keep the
    # watch alive (a false ping is cheap; a swallowed one is the 83¢ bid).
    cmin = corroborated_extreme([f for _, f in day_obs], "low")
    certain = certain_max_settle(cmin) if cmin is not None else None

    out = []
    for m in markets:
        sub = m.get("yes_sub_title") or m.get("subtitle")
        bounds = parse_subtitle(sub)
        if not bounds or bounds[0] is None:  # "or below" has no floor
            continue
        lo = bounds[0]
        if certain is not None and lo > certain:
            continue  # already dead by obs
        gap = latest_f - lo
        if gap > BOUNDARY_APPROACH_F:
            continue
        out.append({
            "ticker": m.get("ticker"), "subtitle": sub, "floor_f": lo,
            "run_min_f": round(run_min, 1), "latest_f": round(latest_f, 1),
            "ob_time": ob_time, "gap_f": round(gap, 1),
            "minutes_left": round(minutes_left),
        })
    return out


def should_alert_boundary(state: dict, ticker: str, gap_f: float) -> bool:
    """First sighting alerts; after that only a BOUNDARY_REALERT_F closer step."""
    prev = state.get(f"boundary:{ticker}", {}).get("gap_f")
    return prev is None or gap_f <= prev - BOUNDARY_REALERT_F + 1e-9


async def watch_boundaries(client, state: dict, dry_run: bool) -> list[dict]:
    """Scan low ladders inside their boundary window; return sent findings."""
    from zoneinfo import ZoneInfo

    from core.obs import climate_day_end, climate_day_start, fetch_day_obs_timed
    from ladders import load_ladders

    now_utc = datetime.now(timezone.utc)
    sent: list[dict] = []
    for lad in load_ladders():
        if lad.kind != "low":
            continue
        tz = ZoneInfo(lad.tz)
        day_end = climate_day_end(tz)
        if not (day_end - timedelta(hours=BOUNDARY_WATCH_HOURS)
                <= now_utc.astimezone(tz) < day_end):
            continue
        try:
            obs = fetch_day_obs_timed(lad.station_icao, tz)
        except Exception as exc:  # noqa: BLE001 — one station must not kill the run
            logger.warning(f"{lad.series}: boundary obs fetch failed: {exc}")
            continue
        markets, ok = await client.get_markets_checked(series_ticker=lad.series)
        if not ok:
            continue
        day_iso = climate_day_start(tz).date().isoformat()
        todays = [m for m in markets
                  if extract_target_date_from_ticker(m.get("ticker", "")) == day_iso]
        for cand in boundary_candidates(todays, obs, now_utc,
                                        day_end.astimezone(timezone.utc)):
            try:
                book = await client.get_orderbook(cand["ticker"])
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"{cand['ticker']}: boundary book fetch failed: {exc}")
                continue
            yes_bids = sorted((book or {}).get("yes") or [], key=lambda lv: -lv[0])
            bid = yes_bids[0][0] if yes_bids else None
            depth = bid_depth_near_best(yes_bids)
            if (bid is None or bid < BOUNDARY_MIN_BID_C
                    or depth < MIN_ALERT_DEPTH):
                continue
            cand.update(bid_c=bid, bid_depth=depth, series=lad.series)
            if dry_run:
                print(f"  BOUNDARY {cand['ticker']}: floor {cand['floor_f']:.0f}° "
                      f"ob {cand['latest_f']}° (gap {cand['gap_f']}°) "
                      f"{cand['minutes_left']}min left, bid {bid}¢ x{depth:.0f}")
                continue
            if not should_alert_boundary(state, cand["ticker"], cand["gap_f"]):
                continue
            sent.append(cand)
    if sent:
        ages = {c["ticker"]: round((now_utc - c["ob_time"]).total_seconds() / 60)
                for c in sent}
        lines = [
            f"**{c['ticker']}** — “{c['subtitle']}” bid **{c['bid_c']}¢** "
            f"({c['bid_depth']:.0f} deep)\n"
            f"  ob {c['latest_f']}° ({ages[c['ticker']]}min ago) vs floor "
            f"{c['floor_f']:.0f}° — gap {c['gap_f']}°, min so far {c['run_min_f']}°, "
            f"**{c['minutes_left']}min to midnight LST**. An ob ≤{c['floor_f'] - 1:.0f}° "
            f"before the boundary kills it; after, it survives to settle."
            for c in sent
        ]
        try:
            from notifications import send_discord_alert

            await send_discord_alert(
                title=f"🌡️ BOUNDARY WATCH — {len(sent)} low bracket(s) near the floor",
                description="\n".join(lines)[:4096],
                color=0x3498DB,
                context="live_watch",
                mention=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"discord boundary alert failed: {exc}")
        for c in sent:
            state[f"boundary:{c['ticker']}"] = {"gap_f": c["gap_f"],
                                                "ts": now_utc.isoformat(timespec="seconds")}
        atomic_write_json(STATE_FILE, state)
        logger.info(f"boundary alert sent for {len(sent)} bracket(s)")
    return sent


def account_snapshot(balance, positions: list[dict], fills: list[dict], now: str) -> dict:
    """Full real-account view for the dashboard: balance, realized P&L (incl.
    closed positions), open/closed positions, recent fills."""
    realized_total = 0.0
    open_pos, closed_pos = [], []
    for p in positions or []:
        try:
            qty = float(p.get("position_fp") or 0)
            realized = float(p.get("realized_pnl_dollars") or 0)
            exposure = float(p.get("market_exposure_dollars") or 0)
        except (TypeError, ValueError):
            continue
        realized_total += realized
        row = {"ticker": p.get("ticker"), "qty": qty,
               "realized": round(realized, 2), "exposure": round(exposure, 2)}
        (open_pos if qty != 0 else closed_pos).append(row)
    recent = []
    for f in sorted(fills or [], key=lambda x: x.get("created_time", ""), reverse=True)[:12]:
        try:
            px = round(float(f.get("yes_price_dollars") or 0) * 100)
        except (TypeError, ValueError):
            px = None
        recent.append({"ts": f.get("created_time"), "ticker": f.get("ticker"),
                       "action": f.get("action"), "price_c": px,
                       "count": f.get("count_fp"), "taker": f.get("is_taker")})
    return {
        "updated": now,
        "balance": round(balance, 2) if balance is not None else None,
        "realized_total": round(realized_total, 2),
        "open_positions": open_pos,
        "closed_positions": [c for c in closed_pos if c["realized"] != 0],
        "recent_fills": recent,
    }


def _append(path: Path, obj: dict) -> None:
    LOGS.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(json.dumps(obj, separators=(",", ":")) + "\n")


def reads_degraded(fills_resp) -> bool:
    """True when the authenticated fills read failed — a real response always
    carries a 'fills' key, while kalshi_client._req_safe degrades errors
    (e.g. 401 from a bad key path) to {}/None. Writing a snapshot from a
    degraded read would show a false $0.00 balance on the dashboard
    (happened live 2026-07-05 after the VPS migration)."""
    return not isinstance(fills_resp, dict) or "fills" not in fills_resp


async def run(threshold: int, dry_run: bool) -> None:
    import os

    from kalshi_client import KalshiClient

    key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH", "")
    if not key_path or not Path(key_path).is_file():
        logger.error(f"private key missing at {key_path!r} — refusing a "
                     "credential-less run (would journal a false $0 snapshot)")
        return

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    client = KalshiClient(
        api_key_id=os.getenv("KALSHI_API_KEY_ID", ""),
        private_key_path=key_path,
        demo_mode=False,
    )
    await client.start()
    try:
        # Boundary watch first: it reads only public endpoints, so it must
        # survive the authenticated-read guard below (an auth failure would
        # otherwise silence the one ping this job exists to send at 11 PM).
        try:
            await watch_boundaries(client, _load_state(), dry_run)
        except Exception as exc:  # noqa: BLE001 — never block the account journal
            logger.warning(f"boundary watch failed: {exc}")

        # None (not 0.0) on a degraded read — a false $0.00 once reached the
        # public equity curve (2026-07-05). The balance endpoint can fail
        # independently of fills, so it needs its own guard.
        balance = await client.get_balance_checked()
        fills_resp = await client._req_safe("GET", "/portfolio/fills?limit=100", auth=True)
        if reads_degraded(fills_resp) or balance is None:
            logger.error("authenticated reads degraded (auth failure?) — "
                         "skipping journal/snapshot writes to protect live_account.json")
            return
        fresh = new_fills((fills_resp or {}).get("fills"), known_fill_ids())
        positions = await client.get_positions()
        longs = open_long_positions(positions)

        strength: list[tuple[dict, int]] = []
        for p in longs:
            try:
                book = await client.get_orderbook(p["ticker"])
            except Exception as exc:  # noqa: BLE001 — one book must not kill the run
                logger.warning(f"{p['ticker']}: book fetch failed: {exc}")
                continue
            yes_bids = sorted((book or {}).get("yes") or [], key=lambda lv: -lv[0])
            bid = yes_bids[0][0] if yes_bids else None
            if bid is not None:
                p["best_bid"] = bid
                p["bid_depth"] = bid_depth_near_best(yes_bids)
                strength.append((p, bid))

        if dry_run:
            print(f"balance ${balance:.2f} | {len(fresh)} new fill(s) | {len(longs)} open long(s)")
            for p, bid in strength:
                real = bid >= threshold and p["bid_depth"] >= MIN_ALERT_DEPTH
                print(f"  {p['ticker']}: {p['qty']:.0f}x, best bid {bid}c "
                      f"(depth {p['bid_depth']:.0f})"
                      f"{'  <-- SELL-INTO-STRENGTH' if real else ''}")
            for p, age in overdue_settlements(positions, datetime.now(timezone.utc).date()):
                print(f"  {p['ticker']}: SETTLEMENT OVERDUE {age}d")
            return

        for f in fresh:
            _append(FILLS_LOG, {"logged_at": now, **f})
        if fresh:
            logger.info(f"journaled {len(fresh)} new live fill(s)")
        if longs:
            _append(POSITIONS_LOG, {"ts": now, "positions": longs})
        if balance is not None and balance != last_logged_balance():
            _append(BALANCE_LOG, {"ts": now, "balance": balance})

        # Latest full snapshot for the dashboard (atomic overwrite).
        LOGS.mkdir(parents=True, exist_ok=True)
        snap = account_snapshot(balance, positions, (fills_resp or {}).get("fills"), now)
        atomic_write_json(ACCOUNT_FILE, snap)

        state = _load_state()
        pings = [(p, bid) for p, bid in strength
                 if should_alert_strength(state, p["ticker"], bid, threshold, p["bid_depth"])]
        if pings:
            lines = [
                f"**{p['ticker']}** — {p['qty']:.0f} contracts, best bid **{bid}¢** "
                f"({p['bid_depth']:.0f} within {DEPTH_BAND_C}¢)\n"
                f"  Your playbook: sell into strength (90¢ now beats $1 tomorrow).\n"
                f"  `.venv/bin/python scripts/take.py {p['ticker']} sell yes "
                f"{p['qty']:.0f} {max(1, bid - 2)} --ioc`"
                for p, bid in pings
            ]
            try:
                from notifications import send_discord_alert

                await send_discord_alert(
                    title=f"📈 LIVE position(s) at {threshold}¢+ — consider selling",
                    description="\n".join(lines)[:4096],
                    color=0xF1C40F,
                    context="live_watch",
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"discord alert failed: {exc}")
            for p, bid in pings:
                state[p["ticker"]] = {"bid": bid, "ts": now}
            atomic_write_json(STATE_FILE, state)
            logger.info(f"strength alert sent for {len(pings)} position(s)")

        today_iso = datetime.now(timezone.utc).date().isoformat()
        overdue = [(p, age) for p, age in
                   overdue_settlements(positions, datetime.now(timezone.utc).date())
                   if should_alert_overdue(state, p["ticker"], today_iso)]
        if overdue:
            lines = [
                f"**{p['ticker']}** — event {age} days past, still unsettled "
                f"(exposure ${parse_dollars(p.get('market_exposure_dollars')):.2f}). "
                f"Settlement doc has existed for days; consider a support ticket."
                for p, age in overdue
            ]
            try:
                from notifications import send_discord_alert

                await send_discord_alert(
                    title=f"⏳ {len(overdue)} position(s) with OVERDUE settlement",
                    description="\n".join(lines)[:4096],
                    color=0x95A5A6,
                    context="live_watch",
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"discord overdue alert failed: {exc}")
            for p, _ in overdue:
                state[f"overdue:{p['ticker']}"] = today_iso
            atomic_write_json(STATE_FILE, state)
            logger.info(f"settlement-overdue alert sent for {len(overdue)} position(s)")
    finally:
        await client.stop()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--once", action="store_true", help="single pass (cron mode)")
    ap.add_argument("--dry-run", action="store_true", help="print, no writes/Discord")
    ap.add_argument("--threshold", type=int, default=DEFAULT_STRENGTH_C,
                    help="best-bid cents that triggers the sell-into-strength ping")
    args = ap.parse_args()
    if not args.once:
        ap.error("only --once mode is supported; schedule via cron")
    asyncio.run(run(args.threshold, args.dry_run))
    write_heartbeat("live_watch")


if __name__ == "__main__":
    main()
