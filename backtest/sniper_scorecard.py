#!/usr/bin/env python3
"""SNIPER SCORECARD — did the CLI sniper's alerts actually have edge?

cli_sniper journals every finding (ticker, kind, economics) to
logs/cli_sniper/*.jsonl but nothing joins them to outcomes. This does the
join — the sniper's equivalent of poly_gate_analyzer — and answers the only
question that should drive further work: does the alert win, and by how much?

Per finding, settlement comes from Kalshi's per-ticker market `result`
(finalized -> 'yes'/'no'; active -> pending), covering all 40 ladders
(daily_data.jsonl only has the 5 original high cities). Scoring, per contract:

  buy_winner  bought the printed bracket at `ask`; realized =
              (100 if result==yes else 0) - ask - taker_fee(ask).
  sell_dead   sold the dead bracket's bids for `net_cents` (already fee-net);
              realized = +net_cents if result==no, else the swept collateral
              is called: -(contracts*100 - net_cents). This is where a
              settlement-source misfire (the KAUS/$348 class) shows up as the
              big loss it would have been.

Splits: is_final (floor vs final — tests whether final-CLI winners settle too
fast to trade), kind, high/low ladder, station. Alert-only; never trades.

Usage:
    python3 backtest/sniper_scorecard.py               # all journal days
    python3 backtest/sniper_scorecard.py --days 7 --report discord

Meaningful only after the sniper cron has run for a week+.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dutch_book import kalshi_taker_fee_cents  # noqa: E402

JOURNAL_DIR = PROJECT_ROOT / "logs" / "cli_sniper"
VERDICT_FILE = HERE / "sniper_scorecard_verdict.json"


def load_findings(journal_dir: Path = JOURNAL_DIR, since: datetime | None = None) -> list[dict]:
    """Flatten journal rows to one dict per finding, carrying parent context."""
    out: list[dict] = []
    if not journal_dir.exists():
        return out
    for path in sorted(journal_dir.glob("*.jsonl")):
        for line in path.read_text().splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = row.get("ts", "")
            if since is not None:
                try:
                    if datetime.fromisoformat(ts) < since:
                        continue
                except ValueError:
                    pass
            for f in row.get("findings") or []:
                out.append({
                    "ts": ts, "awips": row.get("awips"),
                    "summary_date": row.get("summary_date"),
                    "is_final": bool(row.get("is_final")),
                    **f,
                })
    return out


def ladder_kind(series: str) -> str:
    return "low" if (series or "").startswith("KXLOWT") else "high"


def score_finding(finding: dict, result: str | None) -> dict | None:
    """Realized outcome for one finding; None while the market is pending."""
    if result not in ("yes", "no"):
        return None
    kind = finding.get("kind")
    if kind == "buy_winner":
        ask = finding.get("ask")
        if ask is None:
            return None
        size = int(finding.get("ask_depth") or 0) or 1
        won = result == "yes"
        per = (100 if won else 0) - ask - kalshi_taker_fee_cents(ask)
    elif kind == "sell_dead":
        net = finding.get("net_cents")
        contracts = int(finding.get("contracts") or 0)
        if net is None or contracts <= 0:
            return None
        size = contracts
        won = result == "no"
        total = net if won else -(contracts * 100 - net)   # cents
        per = total / contracts
    else:
        return None
    return {
        "ticker": finding.get("ticker"), "kind": kind,
        "is_final": finding.get("is_final"), "awips": finding.get("awips"),
        "ladder": ladder_kind(finding.get("series", "")),
        "won": won, "per_contract_cents": round(per, 2),
        "size": size, "realized_dollars": round(per * size / 100, 2),
    }


def aggregate(scored: list[dict]) -> dict:
    if not scored:
        return {"n": 0, "hit_rate": 0.0, "mean_per_contract_cents": 0.0,
                "total_dollars": 0.0}
    return {
        "n": len(scored),
        "hit_rate": round(sum(1 for s in scored if s["won"]) / len(scored), 3),
        "mean_per_contract_cents": round(
            statistics.fmean(s["per_contract_cents"] for s in scored), 2),
        "total_dollars": round(sum(s["realized_dollars"] for s in scored), 2),
    }


def split_by(scored: list[dict], key) -> dict:
    groups: dict = {}
    for s in scored:
        groups.setdefault(key(s), []).append(s)
    return {str(k): aggregate(v) for k, v in sorted(groups.items(), key=lambda kv: str(kv[0]))}


async def fetch_results(tickers: list[str]) -> dict[str, str | None]:
    """ticker -> 'yes'/'no' (finalized) or None (pending/unknown)."""
    import os

    from kalshi_client import KalshiClient

    client = KalshiClient(
        api_key_id=os.getenv("KALSHI_API_KEY_ID", ""),
        private_key_path=os.getenv("KALSHI_PRIVATE_KEY_PATH", ""),
        demo_mode=False,
    )
    out: dict[str, str | None] = {}
    await client.start()
    try:
        for ticker in tickers:
            try:
                r = await client._req_safe("GET", f"/markets/{ticker}")
            except Exception:  # noqa: BLE001 — one lookup must not kill the run
                out[ticker] = None
                continue
            mk = (r or {}).get("market") or {}
            res = mk.get("result")
            out[ticker] = res if res in ("yes", "no") else None
    finally:
        await client.stop()
    return out


def build(findings: list[dict], results: dict[str, str | None]) -> dict:
    scored, pending = [], 0
    for f in findings:
        s = score_finding(f, results.get(f.get("ticker")))
        if s is None:
            pending += 1
        else:
            scored.append(s)
    return {
        "overall": aggregate(scored), "pending": pending,
        "by_certainty": split_by(scored, lambda s: "final" if s["is_final"] else "floor"),
        "by_kind": split_by(scored, lambda s: s["kind"]),
        "by_ladder": split_by(scored, lambda s: s["ladder"]),
        "by_station": split_by(scored, lambda s: s["awips"]),
        "scored": scored,
    }


def format_report(result: dict) -> str:
    o = result["overall"]
    lines = [f"**Sniper scorecard — {o['n']} settled findings ({result['pending']} pending)**"]
    if o["n"]:
        lines.append(f"overall: hit {o['hit_rate']:.0%}, mean **{o['mean_per_contract_cents']:+.1f}¢**/contract, "
                     f"realized **${o['total_dollars']:+.2f}**")
        for label, split in (("certainty", "by_certainty"), ("kind", "by_kind"),
                             ("ladder", "by_ladder")):
            parts = [f"{k} {v['mean_per_contract_cents']:+.0f}¢×{v['n']}"
                     for k, v in result[split].items()]
            lines.append(f"  {label}: " + ", ".join(parts))
        pay = {k: v for k, v in result["by_station"].items() if v["total_dollars"] != 0}
        top = sorted(pay.items(), key=lambda kv: -kv[1]["total_dollars"])[:5]
        if top:
            lines.append("  top stations $: " + ", ".join(
                f"{k} ${v['total_dollars']:+.0f}" for k, v in top))
    else:
        lines.append("no settled findings yet — needs the sniper cron to accrue "
                     "a few days of data.")
    return "\n".join(lines)


async def main_async(days: int | None, report: str) -> None:
    since = datetime.now(timezone.utc) - timedelta(days=days) if days else None
    findings = load_findings(since=since)
    tickers = sorted({f["ticker"] for f in findings if f.get("ticker")})
    results = await fetch_results(tickers)
    result = build(findings, results)
    VERDICT_FILE.write_text(json.dumps(
        {k: v for k, v in result.items() if k != "scored"}, indent=1) + "\n")
    text = format_report(result)
    if report == "discord":
        try:
            from notifications import send_discord_alert
            await send_discord_alert(title="🎯 Sniper scorecard", description=text[:4096],
                                     color=0xE67E22, context="sniper_scorecard")
        except Exception as exc:  # noqa: BLE001
            print(f"discord send failed: {exc}", file=sys.stderr)
    print(text)
    try:
        from heartbeat import write_heartbeat
        write_heartbeat("sniper_scorecard")
    except Exception:  # noqa: BLE001 — heartbeat must never block the report
        pass


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=int, default=None, help="only findings newer than N days")
    ap.add_argument("--report", choices=("stdout", "discord"), default="stdout")
    args = ap.parse_args()
    import asyncio
    asyncio.run(main_async(args.days, args.report))


if __name__ == "__main__":
    main()
