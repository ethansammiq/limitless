#!/usr/bin/env python3
"""EXPORT PUBLIC STATS — sanitized snapshot for the ethansam.io Trading tab.

Builds logs/public_stats.json from local files only (no network):
live_account.json, live_balance.jsonl, live_fills.jsonl,
backtest/sniper_scorecard_verdict.json, heartbeats.json, ladders.json.

Security contract (asserted, not intended):
  * The payload is CONSTRUCTED from a whitelist — raw records are never
    copied through, so order/fill/trade ids can't leak by omission bugs.
  * Every key in the final payload must be in ALLOWED_KEYS or the export
    raises. Every .env value must be absent from the serialized payload
    or the export raises.
  * Real-money data only — the paper ledger is never read.

Cron: */30 (installed by deploy/setup_oracle.sh). A GitHub Action in the
public limitless repo pulls this file over a forced-command SSH key (which
can ONLY cat this file) and publishes it to the `stats` branch, where the
website fetches it. The site never talks to Kalshi or this box.

Usage:
    python3 scripts/export_public_stats.py            # write + heartbeat
    python3 scripts/export_public_stats.py --dry-run  # print, no writes
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import dotenv_values, load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

from heartbeat import write_heartbeat  # noqa: E402
from log_setup import get_logger  # noqa: E402

logger = get_logger(__name__)

ACCOUNT_FILE = PROJECT_ROOT / "logs" / "live_account.json"
BALANCE_LOG = PROJECT_ROOT / "logs" / "live_balance.jsonl"
FILLS_LOG = PROJECT_ROOT / "logs" / "live_fills.jsonl"
VERDICT_FILE = PROJECT_ROOT / "backtest" / "sniper_scorecard_verdict.json"
HEARTBEATS = PROJECT_ROOT / "heartbeats.json"
OUT_FILE = PROJECT_ROOT / "logs" / "public_stats.json"

START_BANKROLL_USD = 100.00   # the single real-money deposit (2026-07-03)
EQUITY_POINTS_MAX = 500
SNIPER_FRESH_MIN = 30         # cron */2 → anything younger counts as alive

# Every key that may appear anywhere in the payload. A key outside this set
# aborts the export — additions here are a deliberate publishing decision.
ALLOWED_KEYS = {
    "generated_at", "bankroll", "start_usd", "current_usd", "return_pct",
    "equity_curve", "ts", "usd",
    "best_trade", "ticker", "date", "avg_buy_cents", "avg_sell_cents",
    "multiple", "contracts",
    "hunting", "stations_watching_now", "total_ladders", "sniper_alive",
    "next_window", "kind", "starts_in_min", "station",
    "scorecard", "settled", "hit_rate_pct", "mean_cents_per_contract",
    "pending", "as_of",
    "lifetime", "tickers_traded", "realized_usd",
}


def _read_jsonl(path: Path) -> list[dict]:
    rows = []
    if not path.exists():
        return rows
    for line in path.read_text().splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def build_equity_curve(balance_rows: list[dict], account: dict) -> list[dict]:
    pts = [{"ts": r["ts"], "usd": round(float(r["balance"]), 2)}
           for r in balance_rows if r.get("ts") and r.get("balance") is not None]
    bal, upd = account.get("balance"), account.get("updated")
    if bal is not None and upd and (not pts or pts[-1]["usd"] != round(float(bal), 2)):
        pts.append({"ts": upd, "usd": round(float(bal), 2)})
    return pts[-EQUITY_POINTS_MAX:]


def build_best_trade(fills: list[dict]) -> dict | None:
    """Highest sell/buy multiple among tickers with both legs — whitelisted
    fields only; ids and raw records never pass through."""
    by_ticker: dict[str, dict[str, list[tuple[float, float]]]] = {}
    for f in fills:
        t, action = f.get("ticker"), f.get("action")
        try:
            price = float(f.get("yes_price_dollars", 0)) * 100
            count = float(f.get("count_fp", 0))
        except (TypeError, ValueError):
            continue
        if not t or action not in ("buy", "sell") or price <= 0 or count <= 0:
            continue
        by_ticker.setdefault(t, {"buy": [], "sell": []})[action].append((price, count))

    best = None
    for t, legs in by_ticker.items():
        if not legs["buy"] or not legs["sell"]:
            continue
        b_qty = sum(c for _, c in legs["buy"])
        s_qty = sum(c for _, c in legs["sell"])
        avg_buy = sum(p * c for p, c in legs["buy"]) / b_qty
        avg_sell = sum(p * c for p, c in legs["sell"]) / s_qty
        mult = avg_sell / avg_buy
        if best is None or mult > best["multiple"]:
            date = str(sorted(f.get("created_time", "") for f in fills
                              if f.get("ticker") == t)[0])[:10]
            best = {"ticker": t, "date": date,
                    "avg_buy_cents": round(avg_buy, 1),
                    "avg_sell_cents": round(avg_sell, 1),
                    "multiple": round(mult, 1),
                    "contracts": int(s_qty)}
    return best


def build_hunting(now_utc: datetime, heartbeats: dict) -> dict:
    from cli_sniper import stations_in_window, window_kind
    from ladders import by_awips

    groups = by_awips()
    watching = stations_in_window(now_utc, groups)

    soonest = None
    for awips, group in groups.items():
        local = now_utc.astimezone(ZoneInfo(group[0].tz))
        frac = local.hour + local.minute / 60
        open_kind = window_kind(frac)
        if open_kind is not None:
            soonest = {"kind": open_kind, "starts_in_min": 0, "station": awips}
            break
        for start, kind in ((1.0, "morning"), (15.5, "afternoon")):
            delta_h = start - frac if frac < start else start + 24 - frac
            mins = int(delta_h * 60)
            if soonest is None or mins < soonest["starts_in_min"]:
                soonest = {"kind": kind, "starts_in_min": mins, "station": awips}

    sniper_age_min = None
    try:
        ts = heartbeats.get("cli_sniper", {}).get("timestamp")
        if ts:
            sniper_age_min = (now_utc - datetime.fromisoformat(ts)).total_seconds() / 60
    except (ValueError, TypeError):
        pass

    return {"stations_watching_now": len(watching),
            "total_ladders": sum(len(g) for g in groups.values()),
            "sniper_alive": sniper_age_min is not None and sniper_age_min < SNIPER_FRESH_MIN,
            "next_window": soonest}


def build_scorecard(verdict: dict, as_of: str | None) -> dict | None:
    overall = (verdict or {}).get("overall") or {}
    if not overall:
        return None
    return {"settled": overall.get("n", 0),
            "hit_rate_pct": round(100 * (overall.get("hit_rate") or 0), 1),
            "mean_cents_per_contract": overall.get("mean_per_contract_cents"),
            "pending": (verdict or {}).get("pending", 0),
            "as_of": as_of}


def build_payload(now_utc: datetime) -> dict:
    account = json.loads(ACCOUNT_FILE.read_text()) if ACCOUNT_FILE.exists() else {}
    balance_rows = _read_jsonl(BALANCE_LOG)
    fills = _read_jsonl(FILLS_LOG)
    heartbeats = json.loads(HEARTBEATS.read_text()) if HEARTBEATS.exists() else {}
    verdict, verdict_as_of = {}, None
    if VERDICT_FILE.exists():
        verdict = json.loads(VERDICT_FILE.read_text())
        verdict_as_of = datetime.fromtimestamp(
            VERDICT_FILE.stat().st_mtime, tz=timezone.utc).isoformat(timespec="seconds")

    bal = account.get("balance")
    current = round(float(bal), 2) if bal is not None else None
    return {
        "generated_at": now_utc.isoformat(timespec="seconds"),
        "bankroll": {
            "start_usd": START_BANKROLL_USD,
            "current_usd": current,
            "return_pct": round((current - START_BANKROLL_USD) / START_BANKROLL_USD * 100, 1)
            if current is not None else None,
        },
        "equity_curve": build_equity_curve(balance_rows, account),
        "best_trade": build_best_trade(fills),
        "hunting": build_hunting(now_utc, heartbeats),
        "scorecard": build_scorecard(verdict, verdict_as_of),
        "lifetime": {
            "tickers_traded": len({f.get("ticker") for f in fills if f.get("ticker")}),
            "realized_usd": round(current - START_BANKROLL_USD, 2)
            if current is not None else None,
        },
    }


def _all_keys(obj) -> set[str]:
    keys: set[str] = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            keys.add(k)
            keys |= _all_keys(v)
    elif isinstance(obj, list):
        for item in obj:
            keys |= _all_keys(item)
    return keys


def assert_sanitized(payload: dict) -> None:
    """Raise if the payload could leak anything — the contract, enforced."""
    rogue = _all_keys(payload) - ALLOWED_KEYS
    if rogue:
        raise ValueError(f"payload contains non-whitelisted keys: {sorted(rogue)}")
    serialized = json.dumps(payload)
    for name, value in (dotenv_values(PROJECT_ROOT / ".env") or {}).items():
        if value and len(value) > 6 and value in serialized:
            raise ValueError(f"payload contains the value of .env:{name}")
    for pattern in ("-----BEGIN", "PRIVATE KEY", "api.elections"):
        if pattern in serialized:
            raise ValueError(f"payload contains forbidden pattern {pattern!r}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true", help="print, no writes")
    args = ap.parse_args()

    now_utc = datetime.now(timezone.utc)
    payload = build_payload(now_utc)
    assert_sanitized(payload)
    body = json.dumps(payload, indent=1)

    if args.dry_run:
        print(body)
        return

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT_FILE.with_suffix(".json.tmp")
    tmp.write_text(body)
    tmp.replace(OUT_FILE)
    logger.info(f"public stats exported ({len(body)} bytes)")
    write_heartbeat("export_public_stats")


if __name__ == "__main__":
    main()
