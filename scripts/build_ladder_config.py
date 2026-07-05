#!/usr/bin/env python3
"""Generate ladders.json — validated settlement metadata for every Kalshi
weather ladder we watch.

    .venv/bin/python scripts/build_ladder_config.py            # write ladders.json
    .venv/bin/python scripts/build_ladder_config.py --dry-run  # print, no write

For each candidate series (shadow_logger.KALSHI_SERIES keys), the settlement
station comes from Kalshi's OWN series metadata — no hand verification:

    GET /series/{t} -> settlement_sources[0].url
      "https://forecast.weather.gov/product.php?site=LOT&product=CLI&issuedby=MDW"
       -> wfo=LOT (issuing office), awips=MDW (station code)

The NWS observation station is K+awips, validated live against
api.weather.gov/stations/{icao} (which also supplies the IANA timezone).
Series that 404, lack a parseable settlement URL, or fail station validation
are reported to stderr and OMITTED — a human reviews the diff and commits.

Consumers read the committed artifact via ladders.load_ladders(); they never
hit these endpoints. Re-run when Kalshi adds cities.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from shadow_logger import KALSHI_SERIES  # noqa: E402

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
NWS_STATION_URL = "https://api.weather.gov/stations/{icao}"
OUT_FILE = PROJECT_ROOT / "ladders.json"
USER_AGENT = "WeatherEdgeLadderConfig/1.0"

_SETTLE_URL = re.compile(r"site=(\w+).*?issuedby=(\w+)", re.IGNORECASE)


def _get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def parse_settlement_url(url: str) -> tuple[str, str] | None:
    """(wfo, awips) from a forecast.weather.gov CLI product URL."""
    m = _SETTLE_URL.search(url or "")
    if not m:
        return None
    return m.group(1).upper(), m.group(2).upper()


def kind_from_series(series: str) -> str:
    return "low" if series.startswith("KXLOWT") else "high"


def station_icao_from_awips(awips: str) -> str:
    """CONUS 3-letter AWIPS ids map to K-prefixed ICAO; 4-letter pass through."""
    return awips if len(awips) == 4 and awips.startswith("K") else f"K{awips}"


def build(candidates: list[str]) -> tuple[list[dict], list[str]]:
    entries: list[dict] = []
    failures: list[str] = []
    station_cache: dict[str, str | None] = {}   # icao -> tz (None = invalid)

    for series in sorted(candidates):
        try:
            payload = _get_json(f"{KALSHI_BASE}/series/{series}")
        except Exception as exc:  # noqa: BLE001 — 404s and network alike: report
            failures.append(f"{series}: series fetch failed ({exc})")
            continue
        sources = (payload.get("series") or {}).get("settlement_sources") or []
        parsed = parse_settlement_url((sources[0] or {}).get("url", "")) if sources else None
        if parsed is None:
            failures.append(f"{series}: no parseable settlement source "
                            f"({json.dumps(sources)[:120]})")
            continue
        wfo, awips = parsed
        icao = station_icao_from_awips(awips)
        if icao not in station_cache:
            try:
                meta = _get_json(NWS_STATION_URL.format(icao=icao))
                station_cache[icao] = (meta.get("properties") or {}).get("timeZone")
            except Exception as exc:  # noqa: BLE001
                station_cache[icao] = None
                failures.append(f"{series}: NWS station {icao} invalid ({exc})")
        tz = station_cache[icao]
        if not tz:
            if not any(icao in f for f in failures):
                failures.append(f"{series}: NWS station {icao} has no timezone")
            continue
        entries.append({
            "series": series,
            "kind": kind_from_series(series),
            "awips": awips,
            "wfo": wfo,
            "station_icao": icao,
            "tz": tz,
        })
    return entries, failures


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true", help="print, don't write")
    args = ap.parse_args()

    entries, failures = build(list(KALSHI_SERIES.keys()))
    text = json.dumps(entries, indent=1)
    if args.dry_run:
        print(text)
    else:
        OUT_FILE.write_text(text + "\n")
        print(f"wrote {len(entries)} ladders -> {OUT_FILE.name}")

    stations = {e["station_icao"] for e in entries}
    highs = sum(1 for e in entries if e["kind"] == "high")
    print(f"{len(entries)} ladders ({highs} high / {len(entries) - highs} low) "
          f"across {len(stations)} stations", file=sys.stderr)
    if failures:
        print(f"\nSKIPPED {len(failures)} — review:", file=sys.stderr)
        for f in failures:
            print(f"  {f}", file=sys.stderr)


if __name__ == "__main__":
    main()
