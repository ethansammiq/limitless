#!/usr/bin/env python3
"""DRIFT CONDITIONING — does obs-trend-at-print split the floor→final drift?

The drift model's only observed failure mode is the still-warming station:
HOU 2026-07-10 graded drift_prob=1.0 and the final came in +4°F post-print;
the MSP wall read a forecast of 87 against a floor of 85 and won. This
study asks the pre-registration question BEFORE any live conditioning:
if the station was already post-peak at floor-print time (peak_monitor's
own definition: ≥45 min past the running max and ≥1.5°F below it), is the
floor materially safer than when the station was still at/near its peak?

Data is the metar_leak_study cache — no new IEM fetches:
  * CLI archives (cli_{awips}.txt): first same-day floor print + its
    issuance time + the final, per station-day (highs only; the drift
    model prices high ladders only).
  * Raw METAR history (asos_{awips}.csv): hourly T-groups give the obs
    series in tenths of °C — the same precision class the settlement
    uses, no separate obs fetch needed.

Per station-day: obs up to the floor's issuance, peak = LAST occurrence
of the running max (a station still sitting on its plateau is NOT
post-peak), classify post_peak vs still_hot, then split the drift
outcome (final − floor: 0 / +1 / ≥+2) by class. The report carries an
80% station-cluster bootstrap CI on P(drift > 0) per class — same
discipline as the scorecard; per-day iid intervals would flatter.

Ship-or-not rule (registered here, judged by the numbers): live
conditioning is worth wiring only if the classes separate — the
still_hot drift rate's CI floor sits above the post_peak rate's CI
ceiling. Otherwise the blind spot needs a different variable.

Usage:
    python3 backtest/drift_conditioning.py            # all cached stations
    python3 backtest/drift_conditioning.py --stations MSP,HOU
Output: report to stdout + rows to backtest/drift_conditioning.jsonl
"""
from __future__ import annotations

import argparse
import json
import random
import re
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backtest.metar_leak_study import (  # noqa: E402
    CACHE_DIR, fetch_cli_archive, fetch_metar_history, parse_cli_archive)
from core.obs import (  # noqa: E402 — the LIVE classifier's thresholds:
    POST_PEAK_MIN_DROP_F, POST_PEAK_MIN_LAG_MIN, TREND_MIN_OBS)
from ladders import load_ladders  # noqa: E402

OUT_FILE = HERE / "drift_conditioning.jsonl"
MIN_OBS_FOR_CLASS = TREND_MIN_OBS      # thinner obs days don't classify

BOOTSTRAP_N = 2000
BOOTSTRAP_SEED = 7
BOOTSTRAP_LEVEL = 0.80

# hourly T-group: TsTTTsTTT — first triplet is the temperature in tenths °C
_T_GROUP = re.compile(r"\bT([01])(\d{3})[01]\d{3}\b")


def parse_obs_series(csv_text: str) -> list[tuple[datetime, float]]:
    """(utc_time, precise °F) per ob, from the cached raw-METAR CSV T-groups."""
    out = []
    for line in (csv_text or "").splitlines():
        parts = line.split(",", 2)
        if len(parts) != 3 or parts[1] == "valid":
            continue
        m = _T_GROUP.search(parts[2])
        if not m:
            continue
        try:
            t = datetime.strptime(parts[1], "%Y-%m-%d %H:%M").replace(
                tzinfo=timezone.utc)
        except ValueError:
            continue
        tenths = int(m.group(2)) * (-1 if m.group(1) == "1" else 1)
        out.append((t, tenths / 10 * 9 / 5 + 32))
    return out


def trend_at_print(obs: list[tuple[datetime, float]], summary_date: str,
                   floor_issued: datetime, tz: str) -> dict | None:
    """Obs-trend features at the floor's issuance, or None (too thin).

    Peak time is the LAST occurrence of the running max — a station
    plateauing at its high is still hot, not post-peak.
    """
    zone = ZoneInfo(tz)
    day = [(t, f) for t, f in obs
           if t <= floor_issued and t.astimezone(zone).date().isoformat() == summary_date]
    if len(day) < MIN_OBS_FOR_CLASS:
        return None
    peak_f = max(f for _, f in day)
    peak_time = max(t for t, f in day if f == peak_f)
    last_f = day[-1][1]
    lag_min = (floor_issued - peak_time).total_seconds() / 60
    drop_f = peak_f - last_f
    post_peak = lag_min >= POST_PEAK_MIN_LAG_MIN and drop_f >= POST_PEAK_MIN_DROP_F
    return {"peak_f": round(peak_f, 1), "lag_min": round(lag_min),
            "drop_f": round(drop_f, 1), "n_obs": len(day),
            "klass": "post_peak" if post_peak else "still_hot"}


def cluster_ci_p(rows: list[dict], level: float = BOOTSTRAP_LEVEL) -> tuple[float, float] | None:
    """80% station-cluster bootstrap CI on P(drift > 0)."""
    clusters: dict[str, list[int]] = {}
    for r in rows:
        clusters.setdefault(r["awips"], []).append(1 if r["drift"] > 0 else 0)
    groups = list(clusters.values())
    if len(groups) < 2:
        return None
    rng = random.Random(BOOTSTRAP_SEED)
    means = []
    for _ in range(BOOTSTRAP_N):
        sample: list[int] = []
        for _ in range(len(groups)):
            sample.extend(rng.choice(groups))
        means.append(statistics.fmean(sample))
    means.sort()
    alpha = (1 - level) / 2
    return (means[int(alpha * (BOOTSTRAP_N - 1))],
            means[int((1 - alpha) * (BOOTSTRAP_N - 1))])


def summarize(rows: list[dict]) -> dict:
    out = {}
    for klass in ("post_peak", "still_hot"):
        rs = [r for r in rows if r["klass"] == klass]
        if not rs:
            continue
        drifted = sum(1 for r in rs if r["drift"] > 0)
        out[klass] = {
            "n": len(rs),
            "same": sum(1 for r in rs if r["drift"] <= 0),
            "up1": sum(1 for r in rs if r["drift"] == 1),
            "up2plus": sum(1 for r in rs if r["drift"] >= 2),
            "p_drift": round(drifted / len(rs), 3),
            "ci80": cluster_ci_p(rs),
        }
    return out


def separated(summary: dict) -> bool | None:
    """The registered ship rule: still_hot CI floor above post_peak CI ceiling."""
    pp, sh = summary.get("post_peak"), summary.get("still_hot")
    if not (pp and sh and pp["ci80"] and sh["ci80"]):
        return None
    return sh["ci80"][0] > pp["ci80"][1]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--stations", help="comma-separated AWIPS subset")
    ap.add_argument("--days", type=int, default=45, help="history depth on cache miss")
    args = ap.parse_args()

    now_utc = datetime.now(timezone.utc)
    stations: dict[str, tuple[str, str]] = {}
    for lad in load_ladders():
        if lad.kind == "high":                     # the drift model's domain
            stations.setdefault(lad.awips, (lad.station_icao, lad.tz))
    if args.stations:
        keep = {s.strip().upper() for s in args.stations.split(",")}
        stations = {a: v for a, v in stations.items() if a in keep}

    rows: list[dict] = []
    skipped_thin = skipped_no_floor = 0
    for awips, (_, tz) in sorted(stations.items()):
        try:
            cli_days = parse_cli_archive(fetch_cli_archive(awips, False), tz)
            obs = parse_obs_series(fetch_metar_history(awips, args.days,
                                                       now_utc, False))
        except Exception as exc:  # noqa: BLE001 — one station must not kill the study
            print(f"  {awips}: fetch failed ({exc}) — skipped")
            continue
        for date, cli in sorted(cli_days.items()):
            floor, final = cli.get("floor_max"), cli.get("final_max")
            issued = cli.get("floor_issued")
            if floor is None or final is None or issued is None:
                skipped_no_floor += 1
                continue
            trend = trend_at_print(obs, date, issued, tz)
            if trend is None:
                skipped_thin += 1
                continue
            rows.append({"awips": awips, "date": date, "floor": floor,
                         "final": final, "drift": final - floor, **trend})

    with OUT_FILE.open("w") as fh:
        for r in rows:
            fh.write(json.dumps(r, separators=(",", ":")) + "\n")

    summary = summarize(rows)
    print(f"\n{len(rows)} classified station-days → {OUT_FILE} "
          f"({skipped_no_floor} without floor+final, {skipped_thin} obs-thin)")
    for klass, s in summary.items():
        ci = (f"  CI80 P(drift) [{s['ci80'][0]:.1%}, {s['ci80'][1]:.1%}]"
              if s["ci80"] else "")
        print(f"[{klass}] n={s['n']}: same {s['same']}, +1 {s['up1']}, "
              f"+2+ {s['up2plus']}  →  P(drift>0) = {s['p_drift']:.1%}{ci}")
    sep = separated(summary)
    verdict = {True: "SEPARATED — conditioning is worth wiring live",
               False: "NOT separated — the blind spot needs a different variable",
               None: "insufficient clusters to judge"}[sep]
    print(f"\nship rule: {verdict}")


if __name__ == "__main__":
    main()
