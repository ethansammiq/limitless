#!/usr/bin/env python3
"""POSITION BRIEF — one command assembles the full evidence pack for a ticker.

The 2026-07-12 DAL/AUS traps were killed by hand-gathering the same five
things under time pressure: the journal prints, the post-print obs, the DSM,
the live book, and the drift numbers. This assembles all of them in one shot
as paste-ready markdown — for a Claude chat, a phone screen, or your own
eyes. READ-ONLY: no orders, no state, no Discord; every section fails open
so a dead feed never blocks the rest.

    .venv/bin/python scripts/position_brief.py KXHIGHTMIN-26JUL12-T91
    .venv/bin/python scripts/position_brief.py KXHIGHTDAL-26JUL12-B95.5 --days 3
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from core import dsm  # noqa: E402
from core.obs import (certain_min_settle, corroborated_extreme,  # noqa: E402
                      fetch_day_obs)
from ladders import load_ladders  # noqa: E402
from market_timeseries import extract_target_date_from_ticker  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]
JOURNAL_DIRS = {"cli_sniper": PROJECT_ROOT / "logs" / "cli_sniper",
                "metar_sniper": PROJECT_ROOT / "logs" / "metar_sniper"}


def resolve_ladder(ticker: str):
    series = ticker.split("-")[0]
    for lad in load_ladders():
        if lad.series == series:
            return lad
    return None


def journal_rows(journal_dir: Path, days: int) -> list[dict]:
    rows: list[dict] = []
    if not journal_dir.exists():
        return rows
    for path in sorted(journal_dir.glob("*.jsonl"))[-days:]:
        for line in path.read_text().splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def relevant_prints(rows: list[dict], ticker: str, awips: str,
                    station: str, target_date: str | None) -> list[str]:
    """Journal lines that bear on this ticker: same station+day products,
    plus any row containing a finding on the exact ticker."""
    out = []
    for r in rows:
        station_match = (r.get("awips") == awips and
                         (target_date is None or r.get("summary_date") == target_date))
        metar_match = (r.get("station") == station and "tenths_c" in r)
        ticker_match = any(f.get("ticker") == ticker
                           for f in r.get("findings") or [])
        if not (station_match or metar_match or ticker_match):
            continue
        if "summary_date" in r:  # CLI product row
            head = (f"- {r.get('ts', '?')} CLI {r.get('awips')} "
                    f"{r.get('summary_date')} stamp {r.get('stamp', '?')} "
                    f"{'FINAL' if r.get('is_final') else 'floor'} "
                    f"max={r.get('max_f')} min={r.get('min_f')}"
                    + (f" CORRECTION {r['correction']}" if r.get("correction") else ""))
        else:  # METAR 6-hr group row
            head = (f"- {r.get('ts', '?')} METAR {r.get('station')} 6-hr "
                    f"{r.get('kind')} {r.get('tenths_c')} tenths°C = "
                    f"{r.get('temp_f')}°F → {r.get('rounded_f')}°")
        for f in r.get("findings") or []:
            if f.get("ticker") == ticker:
                bits = [f.get("kind", "?")]
                for k in ("ask", "ask_depth", "drift_prob", "drift_ev_c",
                          "dsm", "suppressed", "obs_max_f", "obs_kill",
                          "wall_ask", "net_cents"):
                    if k in f:
                        bits.append(f"{k}={f[k]}")
                head += f"\n    · this ticker: {' '.join(str(b) for b in bits)}"
        out.append(head)
    return out


def fmt_book(book: dict) -> str:
    yes = sorted((book or {}).get("yes") or [], key=lambda lv: -lv[0])[:3]
    no = sorted((book or {}).get("no") or [], key=lambda lv: -lv[0])[:3]
    ask = 100 - no[0][0] if no else None
    bid = yes[0][0] if yes else None
    return (f"bid {bid}¢ / ask {ask}¢ | yes bids {yes or '—'} | "
            f"no bids {no or '—'} (ask depth = top no-bid size)")


async def kalshi_section(ticker: str) -> list[str]:
    import os

    from kalshi_client import KalshiClient

    client = KalshiClient(api_key_id=os.getenv("KALSHI_API_KEY_ID", ""),
                          private_key_path=os.getenv("KALSHI_PRIVATE_KEY_PATH", ""),
                          demo_mode=False)
    lines = []
    await client.start()
    try:
        r = await client._req_safe("GET", f"/markets/{ticker}")
        mk = (r or {}).get("market") or {}
        if mk:
            lines.append(f"- market: {mk.get('yes_sub_title') or mk.get('subtitle')} | "
                         f"status {mk.get('status')} | result "
                         f"{mk.get('result') or 'unsettled'}")
        book = await client.get_orderbook(ticker)
        lines.append(f"- book: {fmt_book(book)}")
    except Exception as exc:  # noqa: BLE001 — brief is fail-open per section
        lines.append(f"- kalshi read failed: {exc}")
    finally:
        await client.stop()
    return lines


def build_brief(ticker: str, days: int) -> str:
    now = datetime.now(timezone.utc)
    lad = resolve_ladder(ticker)
    target_date = extract_target_date_from_ticker(ticker)
    out = [f"## Position brief — `{ticker}`",
           f"_generated {now.isoformat(timespec='seconds')} (read-only)_", ""]

    if lad is None:
        out.append(f"⚠ unknown series {ticker.split('-')[0]!r} — not in ladders.json")
        return "\n".join(out)
    out.append(f"**Ladder:** {lad.series} ({lad.kind}) — station {lad.station_icao} "
               f"/ CLI {lad.awips} ({lad.wfo}) / tz {lad.tz} / target day "
               f"{target_date or '?'}")

    # Staged one-tap entry, if any
    try:
        from core import take_queue
        entries = [e for e in take_queue.load_queue()["entries"].values()
                   if e.get("ticker") == ticker]
        for e in entries:
            out.append(f"**Take queue:** {e['status']} — {e['action']} "
                       f"{e['count']}× @ {e['price_c']}¢ staged {e['ts']} "
                       f"[{e['source']}]")
    except Exception as exc:  # noqa: BLE001
        out.append(f"take queue unreadable: {exc}")

    # Journal prints (the document record — outranks every feed)
    out.append("\n### Journal prints (settlement documents)")
    printed_any = False
    for name, jdir in JOURNAL_DIRS.items():
        lines = relevant_prints(journal_rows(jdir, days), ticker,
                                lad.awips, lad.station_icao, target_date)
        if lines:
            printed_any = True
            out.append(f"_{name}:_")
            out.extend(lines)
    if not printed_any:
        out.append("- none on file for this station/day — NOTHING PRINTED YET "
                   "(journal-first rule: thin-ladder bargains without a print "
                   "are usually the wall's information, not yours)")

    # Live obs vs the day so far
    out.append("\n### Station obs (current climate day)")
    try:
        temps = fetch_day_obs(lad.station_icao, ZoneInfo(lad.tz))
        hi = corroborated_extreme(temps, "high")
        lo = corroborated_extreme(temps, "low")
        out.append(f"- {len(temps)} precise obs | corroborated max "
                   f"{f'{hi:.1f}°F ⇒ settle ≥{certain_min_settle(hi)}°' if hi is not None else '—'}"
                   f" | corroborated min {f'{lo:.1f}°F' if lo is not None else '—'}")
    except Exception as exc:  # noqa: BLE001
        out.append(f"- obs fetch failed: {exc}")

    # DSM — the settlement oracle
    out.append("\n### DSM (final CLI follows this, 85/85)")
    try:
        reports = dsm.fetch_dsm_reports(lad.awips)
        if reports:
            for rep in reports[:2]:
                out.append(f"- {rep}")
        else:
            out.append("- unavailable (IEM refusal or none issued)")
    except Exception as exc:  # noqa: BLE001
        out.append(f"- dsm fetch failed: {exc}")

    # Live market
    out.append("\n### Kalshi (live)")
    out.extend(asyncio.run(kalshi_section(ticker)))

    out.append("\n### House rules checklist")
    out.append("- [ ] journal print exists for this station/day (document > feeds)\n"
               "- [ ] DSM extreme inside the bracket (outside ⇒ veto class)\n"
               "- [ ] obs so far don't already beat the bracket (DAL/AUS 7/12 class)\n"
               "- [ ] no deep ask wall opposing (walls 5-0 — same-side oracle only)\n"
               "- [ ] entry ≤ 20¢ cap; notional ≤ $50; drift_prob treated as ≤0.9")
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("ticker", help="e.g. KXHIGHTMIN-26JUL12-T91")
    ap.add_argument("--days", type=int, default=2,
                    help="journal days to scan (default 2)")
    args = ap.parse_args()
    print(build_brief(args.ticker.upper(), args.days))


if __name__ == "__main__":
    main()
