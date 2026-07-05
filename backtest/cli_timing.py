#!/usr/bin/env python3
"""CLI TIMING — learn each office's real publication window from the journal.

cli_sniper.py polls inside hardcoded windows (AFTERNOON 15:30-18:30 local,
MORNING 05:30-08:30) that were guessed wide. Every journaled product carries
the WMO `stamp` (ddhhmm UTC) = its true issuance time. This decodes those per
office and reports the actual clustering, so the windows can be tightened
(fewer wasted fetches) and a future event-driven trigger knows when to expect
each report. Also measures detection latency (sniper `ts` − issuance).

Read-only over logs/cli_sniper/*.jsonl. Reuses ladders for the AWIPS→(wfo,tz).

Usage:
    python3 backtest/cli_timing.py            # summary over all journal days
    python3 backtest/cli_timing.py --days 30
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
sys.path.insert(0, str(PROJECT_ROOT))

from ladders import load_ladders  # noqa: E402

JOURNAL_DIR = PROJECT_ROOT / "logs" / "cli_sniper"
# The sniper's current hardcoded windows (local fractional hours).
AFTERNOON_WINDOW = (15.5, 18.5)
MORNING_WINDOW = (5.5, 8.5)


def stamp_to_utc(stamp: str, ref: datetime) -> datetime | None:
    """WMO ddhhmm → UTC datetime, taking year/month from a reference time
    (the sniper's run ts, which is within minutes-to-hours of issuance)."""
    if not stamp or len(stamp) != 6 or not stamp.isdigit():
        return None
    day, hour, minute = int(stamp[:2]), int(stamp[2:4]), int(stamp[4:6])
    if not (1 <= day <= 31 and hour < 24 and minute < 60):
        return None
    year, month = ref.year, ref.month
    # Month rollover: a stamp day well above the ref day means last month.
    if day - ref.day > 20:
        month -= 1
        if month == 0:
            month, year = 12, year - 1
    try:
        return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)
    except ValueError:
        return None


def local_hour(dt_utc: datetime, tz: str) -> float:
    loc = dt_utc.astimezone(ZoneInfo(tz))
    return loc.hour + loc.minute / 60


def in_window(local_h: float) -> str | None:
    if AFTERNOON_WINDOW[0] <= local_h < AFTERNOON_WINDOW[1]:
        return "afternoon"
    if MORNING_WINDOW[0] <= local_h < MORNING_WINDOW[1]:
        return "morning"
    return None


def _fmt_hhmm(local_h: float) -> str:
    h, m = int(local_h), round((local_h % 1) * 60)
    return f"{h:02d}:{m:02d}"


def load_observations(journal_dir: Path = JOURNAL_DIR, since: datetime | None = None) -> list[dict]:
    """One dict per journaled product: {awips, is_final, issue_utc, run_ts}."""
    out: list[dict] = []
    if not journal_dir.exists():
        return out
    for path in sorted(journal_dir.glob("*.jsonl")):
        for line in path.read_text().splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            try:
                run_ts = datetime.fromisoformat(row.get("ts", ""))
            except ValueError:
                continue
            if since is not None and run_ts < since:
                continue
            issue = stamp_to_utc(row.get("stamp", ""), run_ts)
            if issue is None:
                continue
            out.append({"awips": row.get("awips"), "is_final": bool(row.get("is_final")),
                        "issue_utc": issue, "run_ts": run_ts})
    return out


def summarize(observations: list[dict]) -> dict:
    """Per-(WFO, kind) local issuance stats + overall detection latency."""
    awips_meta = {lad.awips: (lad.wfo, lad.tz) for lad in load_ladders()}
    groups: dict = {}
    latencies: list[float] = []
    for ob in observations:
        meta = awips_meta.get(ob["awips"])
        if meta is None:
            continue
        wfo, tz = meta
        lh = local_hour(ob["issue_utc"], tz)
        kind = "morning" if ob["is_final"] else "afternoon"
        groups.setdefault((wfo, kind), []).append(lh)
        latencies.append((ob["run_ts"] - ob["issue_utc"]).total_seconds() / 60)

    per_office = {}
    for (wfo, kind), hours in sorted(groups.items()):
        lo, hi = min(hours), max(hours)
        cfg = AFTERNOON_WINDOW if kind == "afternoon" else MORNING_WINDOW
        per_office[f"{wfo}/{kind}"] = {
            "n": len(hours), "earliest": _fmt_hhmm(lo), "median": _fmt_hhmm(statistics.median(hours)),
            "latest": _fmt_hhmm(hi),
            "outside_window": sum(1 for h in hours if not (cfg[0] <= h < cfg[1])),
            "suggest": f"{_fmt_hhmm(max(0, lo - 0.25))}-{_fmt_hhmm(min(24, hi + 0.25))}",
        }
    return {
        "n": len(observations),
        "median_detect_latency_min": round(statistics.median(latencies), 1) if latencies else 0.0,
        "per_office": per_office,
    }


def format_report(summary: dict) -> str:
    lines = [f"**CLI issuance timing — {summary['n']} products, "
             f"median detect latency {summary['median_detect_latency_min']:.0f} min**"]
    if not summary["per_office"]:
        lines.append("no journal data yet — needs sniper cron runs.")
        return "\n".join(lines)
    for office, s in summary["per_office"].items():
        flag = f" ⚠{s['outside_window']} outside current window" if s["outside_window"] else ""
        lines.append(f"  {office}: n={s['n']}, {s['earliest']}–{s['latest']} "
                     f"(med {s['median']}) → suggest {s['suggest']}{flag}")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=int, default=None)
    args = ap.parse_args()
    from datetime import timedelta
    since = datetime.now(timezone.utc) - timedelta(days=args.days) if args.days else None
    print(format_report(summarize(load_observations(since=since))))


if __name__ == "__main__":
    main()
