"""Station-day observations and settlement-certainty bounds.

The one place that answers: "what has this settlement station already
observed today, and what does that make CERTAIN about the CLI settle?"
Extracted from dead_bracket_sweeper 2026-07 so the sweeper, the dashboard
radar, and future probes share one implementation of the safety rules:

  Corroboration — a lone extreme ob could be sensor error (the CHI
  2026-06-07 CLI-vs-METAR blowup was ~13°F); require a second ob within
  CORROBORATION_F. Hourly stations legitimately gap 3-4°F on fast
  warm-ups (KDEN 2026-07-02), so the guard is deliberately loose.

  Rounding backoff — METAR temps carry 0.1°C precision and the CLI
  reports integer °F, so a reported 99.5°F max only makes a 99° settle
  certain, not 100°. Extremes back off ROUNDING_BACKOFF_F before rounding.
"""
from __future__ import annotations

import json
import math
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

NWS_OBS_URL = "https://api.weather.gov/stations/{sid}/observations?start={start}&limit=500"

CORROBORATION_F = 5.0
ROUNDING_BACKOFF_F = 0.1


def certain_min_settle(runmax_f: float) -> int:
    """Lowest integer the CLI max can settle at, given the observed running max."""
    return math.floor(runmax_f - ROUNDING_BACKOFF_F + 0.5)


def certain_max_settle(runmin_f: float) -> int:
    """Highest integer the CLI min can settle at, given the observed running min."""
    return math.ceil(runmin_f + ROUNDING_BACKOFF_F - 0.5)


def corroborated_extreme(values: list[float], kind: str) -> float | None:
    """Running max ("high") or min ("low"), or None when a lone spike
    could be sensor error."""
    if len(values) < 2:
        return None
    ordered = sorted(values, reverse=(kind == "high"))
    extreme, second = ordered[0], ordered[1]
    if abs(extreme - second) > CORROBORATION_F:
        return None
    return extreme


def fetch_day_obs(station_id: str, tz: ZoneInfo, user_agent: str = "WeatherEdgeObs/1.0") -> list[float]:
    """All valid temps (°F) for the station-local calendar day, via NWS API."""
    midnight_local = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    start = midnight_local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    url = NWS_OBS_URL.format(sid=station_id, start=start)
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read())
    temps = []
    for feat in payload.get("features", []):
        val = (feat.get("properties", {}).get("temperature") or {}).get("value")
        if val is not None:
            temps.append(val * 9 / 5 + 32)
    return temps
