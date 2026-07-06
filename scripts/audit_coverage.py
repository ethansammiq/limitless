#!/usr/bin/env python3
"""AUDIT COVERAGE — catch silent degradation the other jobs don't.

Three gap classes, all from data the system already has:

  1. SERIES DRIFT — Kalshi weather series live in the API but absent from
     ladders.json (new-city launches). The one network check; the sniper and
     sweeper only cover what's in ladders.json, so a new ladder is invisible
     until this flags it → re-run scripts/build_ladder_config.py.
  2. PARSE HEALTH — journaled CLI products where BOTH max_f and min_f came
     back None (structure parsed, temperatures didn't) — a format drift signal.
  3. OFFICE SILENCE — WFOs in ladders.json that produced ZERO journaled
     products in the window (office not polled / products never caught /
     parse always failing) — the sniper's blind spots.

Discord-alerts only on gaps; heartbeat always. Alert-only; never trades.

Usage:
    python3 scripts/audit_coverage.py            # window default 7 days
    python3 scripts/audit_coverage.py --days 14 --report discord
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

from heartbeat import write_heartbeat  # noqa: E402
from ladders import load_ladders  # noqa: E402
from log_setup import get_logger  # noqa: E402

logger = get_logger(__name__)

SNIPER_JOURNAL = PROJECT_ROOT / "logs" / "cli_sniper"
WEATHER_PREFIXES = ("KXHIGH", "KXLOWT")

# Live Kalshi weather series deliberately NOT in ladders.json (verified
# against /series metadata 2026-07-05). Aliases are dormant duplicate shells
# (settlement URL byte-identical to an existing ladder, zero open markets);
# the rest settle on something other than a per-station NWS CLI product, so
# obs-based deadness logic cannot apply. Re-check if Kalshi opens markets.
IGNORED_SERIES = {
    "KXHIGHHOU": "alias of KXHIGHTHOU (same HGX/HOU CLI source, no markets)",
    "KXHIGHOU": "alias of KXHIGHTHOU (same HGX/HOU CLI source, no markets)",
    "KXHIGHTEMPDEN": "alias of KXHIGHDEN (same BOU/DEN CLI source, no markets)",
    "KXHIGHNYD": "hourly directional NYC, AccuWeather METAR settlement",
    "KXHIGHUS": "national US high, WPC discussion settlement — not per-station",
}


def missing_series(live_tickers: set[str], laddered: set[str]) -> list[str]:
    """Weather series live on Kalshi but not in ladders.json."""
    live_wx = {t for t in live_tickers if t.startswith(WEATHER_PREFIXES)}
    return sorted(live_wx - laddered - set(IGNORED_SERIES))


def load_journal_products(journal_dir: Path, since: datetime) -> list[dict]:
    products = []
    if not journal_dir.exists():
        return products
    for path in sorted(journal_dir.glob("*.jsonl")):
        for line in path.read_text().splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            try:
                if datetime.fromisoformat(row.get("ts", "")) < since:
                    continue
            except ValueError:
                continue
            products.append(row)
    return products


def parse_health_gaps(products: list[dict]) -> list[str]:
    """AWIPS codes whose products parsed with no temperature at all."""
    bad = {p.get("awips") for p in products
           if p.get("max_f") is None and p.get("min_f") is None}
    return sorted(a for a in bad if a)


def silent_offices(products: list[dict], laddered_wfos: set[str],
                   awips_to_wfo: dict[str, str]) -> list[str]:
    """WFOs in ladders.json that produced zero journaled products."""
    seen = {awips_to_wfo.get(p.get("awips")) for p in products}
    return sorted(laddered_wfos - seen)


async def fetch_live_weather_series() -> set[str]:
    import os

    from kalshi_client import KalshiClient

    client = KalshiClient(
        api_key_id=os.getenv("KALSHI_API_KEY_ID", ""),
        private_key_path=os.getenv("KALSHI_PRIVATE_KEY_PATH", ""),
        demo_mode=False,
    )
    await client.start()
    try:
        r = await client._req_safe("GET", "/series/?category=Climate%20and%20Weather")
        return {s.get("ticker") for s in (r or {}).get("series") or [] if s.get("ticker")}
    finally:
        await client.stop()


def build_report(missing: list[str], parse_gaps: list[str],
                 silent: list[str], product_count: int, days: int,
                 series_check_failed: bool = False) -> tuple[str, bool]:
    gaps = bool(missing or parse_gaps or silent or series_check_failed)
    lines = [f"Coverage audit — {product_count} products over {days}d"]
    if series_check_failed:
        lines.append("• series drift: ⚠ COULD NOT CHECK (live series listing failed) "
                     "— drift status unknown, not verified clean")
    else:
        lines.append(f"• series drift: {len(missing)} live weather series NOT in ladders.json"
                     + (f" → {', '.join(missing)} (review + re-run build_ladder_config.py)" if missing else " ✓"))
    lines.append(f"• parse health: {len(parse_gaps)} office(s) with no-temp parses"
                 + (f" → {', '.join(parse_gaps)}" if parse_gaps else " ✓"))
    lines.append(f"• office silence: {len(silent)} laddered WFO(s) produced nothing"
                 + (f" → {', '.join(silent)}" if silent else " ✓"))
    return "\n".join(lines), gaps


async def main_async(days: int, report: str) -> None:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    ladders = load_ladders()
    laddered = {lad.series for lad in ladders}
    laddered_wfos = {lad.wfo for lad in ladders}
    awips_to_wfo = {lad.awips: lad.wfo for lad in ladders}

    series_check_failed = False
    missing: list[str] = []
    try:
        live = await fetch_live_weather_series()
        missing = missing_series(live, laddered)
    except Exception as exc:  # noqa: BLE001 — network; report as UNKNOWN (fail closed), don't crash
        logger.warning(f"series listing failed: {exc}")
        series_check_failed = True

    products = load_journal_products(SNIPER_JOURNAL, since)
    parse_gaps = parse_health_gaps(products)
    silent = silent_offices(products, laddered_wfos, awips_to_wfo)

    text, has_gaps = build_report(missing, parse_gaps, silent, len(products), days,
                                  series_check_failed)
    print(text)
    if report == "discord" and has_gaps:
        try:
            from notifications import send_discord_alert
            await send_discord_alert(title="🔍 Coverage audit — gaps found",
                                     description=text[:4096], color=0xE74C3C,
                                     context="audit_coverage")
        except Exception as exc:  # noqa: BLE001
            print(f"discord send failed: {exc}", file=sys.stderr)
    write_heartbeat("audit_coverage")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--report", choices=("stdout", "discord"), default="stdout")
    args = ap.parse_args()
    asyncio.run(main_async(args.days, args.report))


if __name__ == "__main__":
    main()
