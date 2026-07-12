#!/usr/bin/env python3
"""METAR LEAK STUDY — size the 6-hourly-group edge from the archives.

Question (2026-07-11, the KMSP `10322` discovery): how often does the
METAR 6-hourly max/min already equal the final CLI value Kalshi settles
on, and how much earlier does it print? Backfills a study file the same
way the 85/85 DSM study answered the veto question — from archives, not
memory. The LIVE journal (logs/metar_sniper/) is never written: it stays
the uncensored record of what the detector saw in real time.

Per station: one IEM AFOS request for the CLI archive (finals + floors),
one IEM ASOS request for the raw METAR history. Both cached on disk under
backtest/cache/ so reruns are free and IEM sees each station once.

Joined per station-day (highs; lows mirrored):
  final     — last final CLI max for the date (settlement)
  floor     — first afternoon CLI floor print (what cli_sniper trades)
  metar     — max over the day's 6-hr max groups, precise-tenths rounding;
              windows straddling local midnight are skipped (ambiguous),
              matching the live detector
  metar_pm  — the afternoon group alone (the ~00Z ob covering the peak)
  lead_min  — final-CLI issuance minus the obs time of the group that
              set `metar` (how early the leak prints)

Money metrics reported:
  * P(metar == final) exact and same-bracket (2°F Kalshi brackets)
  * drift resolution: among days where floor != final (the 14% the drift
    model can only price probabilistically), how often the afternoon
    METAR group already named the final — that is the tradeable claim.

Usage:
    python3 backtest/metar_leak_study.py                # all 20 stations
    python3 backtest/metar_leak_study.py --stations MSP,MIA --days 45
    python3 backtest/metar_leak_study.py --refresh      # ignore cache
Output: report to stdout + rows to backtest/metar_leak.jsonl
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
sys.path.insert(0, str(PROJECT_ROOT))

from cli_sniper import parse_product  # noqa: E402
from core.metar import _PK_WND, _SIX_HR, SixHrExtreme, climate_date  # noqa: E402
from ladders import load_ladders  # noqa: E402

CACHE_DIR = HERE / "cache"
OUT_FILE = HERE / "metar_leak.jsonl"

AFOS_URL = ("https://mesonet.agron.iastate.edu/cgi-bin/afos/retrieve.py"
            "?pil=CLI{awips}&fmt=text&limit=120")
ASOS_URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py?{query}"
USER_AGENT = "WeatherEdgeMETARLeakStudy/1.0"
FETCH_GAP_S = 4.0          # IEM is aggressively rate-limited — space requests
BRACKET_F = 2              # Kalshi ladder bracket width


def _get(url: str, timeout: int = 60) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def _cached(name: str, fetch, refresh: bool) -> str:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / name
    if path.exists() and not refresh:
        return path.read_text()
    time.sleep(FETCH_GAP_S)
    text = fetch()
    path.write_text(text)
    return text


def fetch_cli_archive(awips: str, refresh: bool) -> str:
    return _cached(f"cli_{awips}.txt",
                   lambda: _get(AFOS_URL.format(awips=awips)), refresh)


def fetch_metar_history(awips: str, days: int, now_utc: datetime,
                        refresh: bool) -> str:
    start = now_utc - timedelta(days=days)
    query = urllib.parse.urlencode({
        "station": awips, "data": "metar",
        "year1": start.year, "month1": start.month, "day1": start.day,
        "year2": now_utc.year, "month2": now_utc.month, "day2": now_utc.day,
        "tz": "Etc/UTC", "format": "onlycomma", "latlon": "no", "elev": "no",
        "missing": "M", "trace": "T", "direct": "no", "report_type": 3,
    })
    return _cached(f"asos_{awips}.csv",
                   lambda: _get(ASOS_URL.format(query=query)), refresh)


# ---------- CLI archive → per-date finals and first floors ----------

_PRODUCT_SPLIT = re.compile(r"\x01|\n(?=\d{3}\s*\n\w{6}\s+K\w{3}\s+\d{6})")


def archived_stamp_to_utc(stamp: str, summary_date: str) -> datetime | None:
    """WMO ddhhmm resolved against the product's OWN summary date.

    cli_timing.stamp_to_utc anchors on `now`, which mis-months products
    pulled from a weeks-deep archive (and with it the floor/final split).
    A CLI issues on its summary date (floor) or within a couple of days
    after it (final, straggler, correction) — try those.
    """
    if len(stamp) != 6 or not stamp.isdigit():
        return None
    day, hour, minute = int(stamp[:2]), int(stamp[2:4]), int(stamp[4:6])
    try:
        base = datetime.strptime(summary_date, "%Y-%m-%d")
    except ValueError:
        return None
    for offset in range(0, 4):
        cand = base + timedelta(days=offset)
        if cand.day == day:
            try:
                return cand.replace(hour=hour, minute=minute,
                                    tzinfo=timezone.utc)
            except ValueError:
                return None
    return None


def parse_cli_archive(blob: str, tz: str) -> dict[str, dict]:
    """date → {final_max, final_min, floor_max, floor_min, final_issued}.

    Finality = summary date before the STATION-LOCAL issuance date (the
    live sniper's calendar rule — a late Pacific floor rolls past 00Z);
    the floor is the FIRST same-day print (what an alert would have fired
    on). Later finals overwrite (corrections).
    """
    zone = ZoneInfo(tz)
    out: dict[str, dict] = defaultdict(dict)
    for chunk in _PRODUCT_SPLIT.split(blob or ""):
        p = parse_product(chunk)
        if p is None:
            continue
        issued = archived_stamp_to_utc(p.stamp, p.summary_date)
        if issued is None:
            continue
        slot = out[p.summary_date]
        if p.summary_date < issued.astimezone(zone).date().isoformat():
            slot["final_max"], slot["final_min"] = p.max_f, p.min_f
            slot["final_issued"] = issued
        else:
            slot.setdefault("floor_max", p.max_f)
            slot.setdefault("floor_min", p.min_f)
    return dict(out)


# ---------- ASOS CSV → per-date 6-hr extremes ----------

def parse_asos_csv(csv_text: str, station_icao: str) -> list[SixHrExtreme]:
    """SixHrExtremes from IEM's onlycomma dump (station,valid,metar)."""
    out = []
    for line in (csv_text or "").splitlines():
        parts = line.split(",", 2)
        if len(parts) != 3 or parts[1] == "valid":
            continue
        try:
            obs = datetime.strptime(parts[1], "%Y-%m-%d %H:%M").replace(
                tzinfo=timezone.utc)
        except ValueError:
            continue
        _, sep, remarks = parts[2].partition("RMK")
        if not sep:
            continue
        for m in _SIX_HR.finditer(_PK_WND.sub(" ", remarks)):
            tenths = int(m.group(3)) * (-1 if m.group(2) == "1" else 1)
            out.append(SixHrExtreme(
                station=station_icao, obs_time_utc=obs,
                kind="max" if m.group(1) == "1" else "min",
                tenths_c=tenths))
    return out


def daily_metar_extremes(extremes: list[SixHrExtreme], tz: str,
                         kind: str) -> dict[str, dict]:
    """date → {value (rounded °F), obs_time, pm_value} for one kind.

    `value` is the day's extreme across its non-straddling 6-hr groups
    (max of maxes / min of mins); `pm_value` is the group whose window
    ends after 15:00 local — the afternoon leak the detector races.
    """
    zone = ZoneInfo(tz)
    days: dict[str, dict] = {}
    better = max if kind == "max" else min
    for e in extremes:
        if e.kind != kind:
            continue
        day = climate_date(e, tz)
        if day is None:
            continue
        v = e.temp_f_rounded
        slot = days.setdefault(day, {"value": v, "obs_time": e.obs_time_utc})
        if better(v, slot["value"]) == v and v != slot["value"]:
            slot["value"], slot["obs_time"] = v, e.obs_time_utc
        if e.obs_time_utc.astimezone(zone).hour >= 15:
            prev = slot.get("pm_value")
            slot["pm_value"] = v if prev is None else better(prev, v)
    return days


# ---------- join + report ----------

def join_station(awips: str, tz: str, cli_days: dict[str, dict],
                 metar_max: dict[str, dict],
                 metar_min: dict[str, dict]) -> list[dict]:
    rows = []
    for date, cli in sorted(cli_days.items()):
        for kind, metar_days, fkey, flkey in (
                ("high", metar_max, "final_max", "floor_max"),
                ("low", metar_min, "final_min", "floor_min")):
            final = cli.get(fkey)
            md = metar_days.get(date)
            if final is None or md is None:
                continue
            row = {"awips": awips, "date": date, "kind": kind,
                   "final": final, "floor": cli.get(flkey),
                   "metar": md["value"], "metar_pm": md.get("pm_value"),
                   "metar_obs": md["obs_time"].isoformat(timespec="minutes")}
            issued = cli.get("final_issued")
            if issued:
                row["lead_min"] = round(
                    (issued - md["obs_time"]).total_seconds() / 60)
            rows.append(row)
    return rows


def summarize(rows: list[dict]) -> dict:
    """Headline numbers per ladder kind."""
    out = {}
    for kind in ("high", "low"):
        rs = [r for r in rows if r["kind"] == kind]
        if not rs:
            continue
        exact = sum(1 for r in rs if r["metar"] == r["final"])
        within_bracket = sum(1 for r in rs
                             if abs(r["metar"] - r["final"]) < BRACKET_F)
        drift_days = [r for r in rs
                      if r.get("floor") is not None and r["floor"] != r["final"]]
        drift_called = sum(1 for r in drift_days
                           if r.get("metar_pm") == r["final"]
                           or r["metar"] == r["final"])
        leads = sorted(r["lead_min"] for r in rs if "lead_min" in r)
        out[kind] = {
            "n": len(rs), "exact": exact,
            "exact_rate": round(exact / len(rs), 3),
            "within_bracket": within_bracket,
            "drift_days": len(drift_days), "drift_called": drift_called,
            "median_lead_min": leads[len(leads) // 2] if leads else None,
        }
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--stations", help="comma-separated AWIPS subset")
    ap.add_argument("--days", type=int, default=45, help="METAR history depth")
    ap.add_argument("--refresh", action="store_true", help="ignore disk cache")
    args = ap.parse_args()

    now_utc = datetime.now(timezone.utc)
    stations: dict[str, tuple[str, str]] = {}   # awips -> (icao, tz)
    for lad in load_ladders():
        stations.setdefault(lad.awips, (lad.station_icao, lad.tz))
    if args.stations:
        keep = {s.strip().upper() for s in args.stations.split(",")}
        stations = {a: v for a, v in stations.items() if a in keep}

    all_rows: list[dict] = []
    for awips, (icao, tz) in sorted(stations.items()):
        try:
            cli_blob = fetch_cli_archive(awips, args.refresh)
            asos_csv = fetch_metar_history(awips, args.days, now_utc,
                                           args.refresh)
        except Exception as exc:  # noqa: BLE001 — one station must not kill the study
            print(f"  {awips}: fetch failed ({exc}) — skipped")
            continue
        cli_days = parse_cli_archive(cli_blob, tz)
        extremes = parse_asos_csv(asos_csv, icao)
        rows = join_station(awips, tz, cli_days,
                            daily_metar_extremes(extremes, tz, "max"),
                            daily_metar_extremes(extremes, tz, "min"))
        all_rows += rows
        print(f"  {awips}: {len(cli_days)} CLI days, "
              f"{len(extremes)} 6-hr groups, {len(rows)} joined rows")

    with OUT_FILE.open("w") as fh:
        for r in all_rows:
            fh.write(json.dumps(r, separators=(",", ":")) + "\n")

    print(f"\n{len(all_rows)} station-day rows → {OUT_FILE}")
    for kind, s in summarize(all_rows).items():
        print(f"\n[{kind.upper()}] n={s['n']}")
        print(f"  METAR == final CLI:      {s['exact']}/{s['n']} "
              f"({s['exact_rate']:.1%})")
        print(f"  within one bracket (±1): {s['within_bracket']}/{s['n']}")
        print(f"  drift days (floor≠final): {s['drift_days']}, "
              f"METAR called the final on {s['drift_called']}")
        print(f"  median lead vs final CLI: {s['median_lead_min']} min")


if __name__ == "__main__":
    main()
