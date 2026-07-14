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
              (100 if result==yes else 0) - ask - kalshi_taker_fee(ask).
  sell_dead   sold the dead bracket's bids for `net_cents` (already fee-net);
              realized = +net_cents if result==no, else the swept collateral
              is called: -(contracts*100 - net_cents). This is where a
              settlement-source misfire (the KAUS/$348 class) shows up as the
              big loss it would have been.

Sizes are capped at the take.py notional clamp (TAKE_MAX_NOTIONAL, $50):
alerts quote full book depth (154k×1¢ walls, 2026-07-12), but no order past
the clamp is executable, so uncapped "realized $" was fiction dominated by
walls nobody could sweep. Per-contract means (the gate currency) are
unaffected.

Splits: is_final (floor vs final — tests whether final-CLI winners settle too
fast to trade), kind, edge class (kind × finality — the unit the gates reason
in), high/low ladder, station. Means carry an 80% cluster-bootstrap CI
resampled by STATION-NIGHT (same-night findings are correlated; iid intervals
are overconfident) — the pivot and daemon gates read these bounds.
Alert-only; never trades.

Usage:
    python3 backtest/sniper_scorecard.py               # all journal days
    python3 backtest/sniper_scorecard.py --days 7 --report discord

Meaningful only after the sniper cron has run for a week+.
"""
from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.fees import kalshi_taker_fee_cents  # noqa: E402

JOURNAL_DIR = PROJECT_ROOT / "logs" / "cli_sniper"
METAR_JOURNAL_DIR = PROJECT_ROOT / "logs" / "metar_sniper"
VERDICT_FILE = HERE / "sniper_scorecard_verdict.json"
METAR_VERDICT_FILE = HERE / "metar_scorecard_verdict.json"


def _row_finality(row: dict, tz_by_awips: dict[str, str]) -> str | None:
    """Recompute the product's finality from the calendar — the same rule
    cli_sniper enforces live since the 2026-07-05 fix. Three bug-era rows
    (same-day 07:31-local products regex-marked FINAL) alerted false 1¢
    certain-winners; scoring them would pollute the pivot-gate sample, so
    history is judged by the corrected rule. None = insufficient fields,
    trust the row as journaled."""
    from cli_sniper import ParsedCLI, effective_finality

    tz = tz_by_awips.get(row.get("awips", ""))
    stamp, ts = row.get("stamp"), row.get("ts", "")
    if not tz or not stamp:
        return None
    try:
        ref = datetime.fromisoformat(ts)
    except ValueError:
        return None
    parsed = ParsedCLI(awips=row["awips"], stamp=stamp,
                       summary_date=row.get("summary_date", ""),
                       is_final=bool(row.get("is_final")),
                       max_f=row.get("max_f"), min_f=row.get("min_f"))
    return effective_finality(parsed, tz, ref)


def load_findings(journal_dir: Path = JOURNAL_DIR, since: datetime | None = None) -> list[dict]:
    """Flatten journal rows to one dict per finding, carrying parent context.

    Rows whose product cannot legitimately classify (same-day pre-afternoon
    issues) are EXCLUDED, and floor/final is recomputed from the calendar,
    so the scorecard judges history by the same rule as live code.
    """
    from ladders import by_awips

    tz_by_awips = {a: g[0].tz for a, g in by_awips().items()}
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
            if not row.get("findings"):
                continue
            finality = _row_finality(row, tz_by_awips)
            if finality == "skip":
                continue  # bug-era intraday product — never classifiable
            is_final = (finality == "final") if finality else bool(row.get("is_final"))
            for f in row.get("findings") or []:
                out.append({
                    "ts": ts, "awips": row.get("awips"),
                    "summary_date": row.get("summary_date"),
                    "is_final": is_final,
                    **{**f, "final": is_final},
                })
    return out


def load_metar_findings(journal_dir: Path = METAR_JOURNAL_DIR,
                        since: datetime | None = None) -> list[dict]:
    """Flatten metar_sniper journal rows to the scorecard's finding shape.

    Suppressed low-ladder buys are KEPT (unlike alert_decay's loader):
    the scorecard measures every journaled finding uncensored — that is
    exactly how the CLI low-floor class was condemned at -30.8¢. Every
    METAR finding is floor-class (is_final=False); cluster keys come from
    the row's station (→ awips) and the finding's own ticker date.
    """
    from ladders import by_station
    from market_timeseries import extract_target_date_from_ticker

    awips_by_icao = {icao: g[0].awips for icao, g in by_station().items()}
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
                    "ts": ts,
                    "awips": awips_by_icao.get(row.get("station", "")),
                    "summary_date": extract_target_date_from_ticker(
                        f.get("ticker", "")),
                    "is_final": False,
                    **{**f, "final": False},
                })
    return out


def ladder_kind(series: str) -> str:
    return "low" if (series or "").startswith("KXLOWT") else "high"


def max_notional_dollars() -> float:
    """The executable-size cap — same env override as scripts/take.py."""
    import os
    try:
        return float(os.getenv("TAKE_MAX_NOTIONAL", 50.0))
    except ValueError:
        return 50.0


def clamp_size(size: int, collateral_per_contract_c: float) -> int:
    """Largest executable count: worst-case collateral fits the notional cap."""
    if collateral_per_contract_c <= 0:
        return size
    return max(1, min(size, int(max_notional_dollars() * 100
                                / collateral_per_contract_c)))


def score_finding(finding: dict, result: str | None) -> dict | None:
    """Realized outcome for one finding; None while the market is pending."""
    if result not in ("yes", "no"):
        return None
    kind = finding.get("kind")
    if kind == "buy_winner":
        ask = finding.get("ask")
        if ask is None:
            return None
        # collateral per contract = entry cost (same leg take.py validates)
        size = clamp_size(int(finding.get("ask_depth") or 0) or 1, ask)
        won = result == "yes"
        per = (100 if won else 0) - ask - kalshi_taker_fee_cents(ask)
    elif kind == "sell_dead":
        net = finding.get("net_cents")
        contracts = int(finding.get("contracts") or 0)
        if net is None or contracts <= 0:
            return None
        # collateral per contract = the complement actually at risk if the
        # "dead" bracket settles YES — the scorecard's own loss model
        size = clamp_size(contracts, 100 - net / contracts)
        won = result == "no"
        total = net if won else -(contracts * 100 - net)   # cents
        per = total / contracts
    else:
        return None
    return {
        "ticker": finding.get("ticker"), "kind": kind,
        "is_final": finding.get("is_final"), "awips": finding.get("awips"),
        "summary_date": finding.get("summary_date"),
        "ladder": ladder_kind(finding.get("series", "")),
        "won": won, "per_contract_cents": round(per, 2),
        "size": size, "realized_dollars": round(per * size / 100, 2),
    }


BOOTSTRAP_LEVEL = 0.80   # the pre-registered gates speak in 80% CIs
BOOTSTRAP_N = 2000
BOOTSTRAP_SEED = 7       # fixed: the weekly report must be reproducible


def cluster_bootstrap_ci(scored: list[dict], level: float = BOOTSTRAP_LEVEL,
                         n_boot: int = BOOTSTRAP_N,
                         seed: int = BOOTSTRAP_SEED) -> dict | None:
    """CI on mean ¢/contract, resampling STATION-NIGHTS, not findings.

    Same-night findings are correlated (2026-07-11: the sample unit is
    nights/regimes — one convective evening moves every bracket it touched),
    so an iid bootstrap over findings understates the interval and lets both
    pre-registered gates mis-fire. Clusters = (awips, summary_date); fewer
    than two clusters has no between-night variance to measure → None.
    """
    clusters: dict[str, list[float]] = {}
    for s in scored:
        key = f"{s.get('awips')}:{s.get('summary_date')}"
        clusters.setdefault(key, []).append(s["per_contract_cents"])
    groups = list(clusters.values())
    if len(groups) < 2:
        return None
    rng = random.Random(seed)
    means = []
    for _ in range(n_boot):
        sample: list[float] = []
        for _ in range(len(groups)):
            sample.extend(rng.choice(groups))
        means.append(statistics.fmean(sample))
    means.sort()
    alpha = (1 - level) / 2
    lo = means[int(alpha * (n_boot - 1))]
    hi = means[int((1 - alpha) * (n_boot - 1))]
    return {"level": level, "lo": round(lo, 1), "hi": round(hi, 1),
            "clusters": len(groups)}


def aggregate(scored: list[dict]) -> dict:
    if not scored:
        return {"n": 0, "hit_rate": 0.0, "mean_per_contract_cents": 0.0,
                "total_dollars": 0.0, "ci80": None}
    return {
        "n": len(scored),
        "hit_rate": round(sum(1 for s in scored if s["won"]) / len(scored), 3),
        "mean_per_contract_cents": round(
            statistics.fmean(s["per_contract_cents"] for s in scored), 2),
        "total_dollars": round(sum(s["realized_dollars"] for s in scored), 2),
        "ci80": cluster_bootstrap_ci(scored),
    }


def finding_class(s: dict) -> str:
    """The edge class a finding belongs to — the unit the gates reason in.

    buy_winner/final = the CLI-FINAL class the daemon gate is registered on;
    buy_winner/floor = the drift class; sell_dead/* = the dead-bracket class.
    A blended overall mean can hide one good class and one bad one.
    """
    return f"{s['kind']}/{'final' if s['is_final'] else 'floor'}"


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
        "by_class": split_by(scored, finding_class),
        "by_ladder": split_by(scored, lambda s: s["ladder"]),
        "by_station": split_by(scored, lambda s: s["awips"]),
        "scored": scored,
    }


def _ci_str(agg: dict) -> str:
    ci = agg.get("ci80")
    if not ci:
        return ""
    return f" CI[{ci['lo']:+.0f},{ci['hi']:+.0f}]¢ ({ci['clusters']} stn-nights)"


def format_report(result: dict) -> str:
    o = result["overall"]
    lines = [f"**Sniper scorecard — {o['n']} settled findings ({result['pending']} pending)**"]
    if o["n"]:
        lines.append(f"overall: hit {o['hit_rate']:.0%}, mean **{o['mean_per_contract_cents']:+.1f}¢**/contract, "
                     f"realized **${o['total_dollars']:+.2f}**{_ci_str(o)}")
        for k, v in result.get("by_class", {}).items():
            lines.append(f"  class {k}: hit {v['hit_rate']:.0%}, "
                         f"{v['mean_per_contract_cents']:+.1f}¢×{v['n']}{_ci_str(v)}")
        for label, split in (("certainty", "by_certainty"), ("kind", "by_kind"),
                             ("ladder", "by_ladder")):
            parts = [f"{k} {v['mean_per_contract_cents']:+.0f}¢×{v['n']}"
                     for k, v in result[split].items()]
            lines.append(f"  {label}: " + ", ".join(parts))
        # Pre-registered gate readouts — informational until their dates;
        # the thresholds themselves are NOT renegotiated here.
        if o.get("ci80"):
            lines.append(f"  pivot-gate readout (2026-08-02): overall upper "
                         f"bound {o['ci80']['hi']:+.1f}¢ vs +2¢ threshold")
        daemon = result.get("by_class", {}).get("buy_winner/final", {})
        if daemon.get("ci80"):
            lines.append(f"  daemon-gate readout: buy_winner/final lower "
                         f"bound {daemon['ci80']['lo']:+.1f}¢ vs >0 threshold")
        pay = {k: v for k, v in result["by_station"].items() if v["total_dollars"] != 0}
        top = sorted(pay.items(), key=lambda kv: -kv[1]["total_dollars"])[:5]
        if top:
            lines.append("  top stations $: " + ", ".join(
                f"{k} ${v['total_dollars']:+.0f}" for k, v in top))
    else:
        lines.append("no settled findings yet — needs the sniper cron to accrue "
                     "a few days of data.")
    return "\n".join(lines)


async def main_async(days: int | None, report: str,
                     journal: str = "cli") -> None:
    since = datetime.now(timezone.utc) - timedelta(days=days) if days else None
    findings = (load_metar_findings(since=since) if journal == "metar"
                else load_findings(since=since))
    tickers = sorted({f["ticker"] for f in findings if f.get("ticker")})
    results = await fetch_results(tickers)
    result = build(findings, results)
    verdict_file = METAR_VERDICT_FILE if journal == "metar" else VERDICT_FILE
    verdict_file.write_text(json.dumps(
        {k: v for k, v in result.items() if k != "scored"}, indent=1) + "\n")
    text = format_report(result)
    if journal == "metar":
        text = text.replace("Sniper scorecard", "METAR sniper scorecard", 1)
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
        write_heartbeat("sniper_scorecard" if journal == "cli"
                        else "metar_scorecard")
    except Exception:  # noqa: BLE001 — heartbeat must never block the report
        pass


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=int, default=None, help="only findings newer than N days")
    ap.add_argument("--report", choices=("stdout", "discord"), default="stdout")
    ap.add_argument("--journal", choices=("cli", "metar"), default="cli",
                    help="which sniper journal to score (default: cli)")
    args = ap.parse_args()
    import asyncio
    asyncio.run(main_async(args.days, args.report, args.journal))


if __name__ == "__main__":
    main()
