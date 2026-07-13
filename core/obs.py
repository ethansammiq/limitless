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

  Climate-day boundary — CLI climate days run midnight-to-midnight in
  LOCAL STANDARD TIME. During daylight saving, an ob at 00:30 wall clock
  belongs to YESTERDAY's climate day. Caught live 2026-07-04: a 75.2°F
  post-midnight-CDT reading made the sweeper call New Orleans "76-77"
  dead while the CLI printed a min of 76 — the market's 93¢ bid was
  right and the naive window was a $195 losing "riskless" trade.
"""
from __future__ import annotations

import json
import math
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

NWS_OBS_URL = "https://api.weather.gov/stations/{sid}/observations?start={start}&limit=500"

# A "certain" verdict needs the extreme CORROBORATED within this of a second
# reading. History: started 2.0, loosened to 5.0 for KDEN's legitimate 3.9°F
# hourly warm-up gaps (2026-07-02), tightened to 1.0 after a lone 75.2°F
# down-spike between continuous 77.0°F readings at KMSY produced a false
# "riskless" $195 dead-bracket call (2026-07-04, CLI printed 76). Real
# extremes are approached twice; spikes aren't. The cost — no verdict for
# ~an hour on fast hourly-station warm-ups — is the right side of the trade
# for a detector whose false positives are losing "riskless" orders.
CORROBORATION_F = 1.0
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


def climate_day_start(tz: ZoneInfo, now: datetime | None = None) -> datetime:
    """Start of the current CLI climate day: midnight LOCAL STANDARD TIME.

    Wall clock 01:00 while daylight saving is active (e.g. CDT), 00:00
    otherwise (and always 00:00 in Phoenix)."""
    now_local = now.astimezone(tz) if now else datetime.now(tz)
    dst = now_local.dst() or timedelta(0)
    return now_local.replace(hour=0, minute=0, second=0, microsecond=0) + dst


def is_precise_celsius(celsius: float) -> bool:
    """Only 0.1°C-resolution (METAR T-group) readings support certainty math.

    The NWS 5-minute feed quantizes many entries to integer °C (±0.9°F).
    Caught live 2026-07-04: KAUS reported a sustained "75.2°F" (= 24.0°C
    exactly) pre-dawn min while the 11:53Z METAR read 75.9 and the CLI
    printed 76 — the integer-°C floor manufactured a $348 false dead-bracket
    call. Integral values are discarded; the occasional genuine x.0°C
    T-group reading goes with them (neighbors corroborate anyway)."""
    return abs(celsius - round(celsius)) > 1e-6


def annotate_floor_buys(entries: list[dict], corroborated_max: float | None,
                        raw_max: float | None) -> None:
    """Stamp floor high-ladder buy findings with what the station ALREADY
    observed. Shared by both snipers (2026-07-13: the METAR path staged a
    day of warming-trap buttons because only cli_sniper had this).

    Two tiers — the corroboration guard is tuned for placing ORDERS, but a
    warning has inverted costs: corroborated exceedance of the bracket is a
    hard obs_kill; a lone precise ob beating it is a soft obs_warn (KDFW's
    real 96.98 peak sat 3.1°F above the next hourly ob and named the final).
    Either keeps the alert but is never staged for one-tap execution.
    """
    from core.brackets import parse_subtitle

    if raw_max is None:
        return
    for e in entries:
        if (e.get("kind") != "buy_winner" or e.get("final")
                or e.get("ladder_kind") != "high"):
            continue
        e["obs_max_f"] = round(raw_max, 1)
        bounds = parse_subtitle(e.get("subtitle"))
        hi = bounds[1] if bounds else None
        if hi is None:
            continue
        if (corroborated_max is not None
                and certain_min_settle(corroborated_max) > hi):
            e["obs_kill"] = (f"obs already {corroborated_max:.1f}° ⇒ settle "
                             f"≥{certain_min_settle(corroborated_max)}° — "
                             f"bracket dead")
        elif certain_min_settle(raw_max) > hi:
            e["obs_warn"] = (f"lone ob {raw_max:.1f}° ⇒ would settle "
                             f"≥{certain_min_settle(raw_max)}° — uncorroborated, "
                             f"verify before buying")


def fetch_day_obs(station_id: str, tz: ZoneInfo, user_agent: str = "WeatherEdgeObs/1.0") -> list[float]:
    """Precise valid temps (°F) for the station's current CLI climate day."""
    start = climate_day_start(tz).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    url = NWS_OBS_URL.format(sid=station_id, start=start)
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read())
    temps = []
    for feat in payload.get("features", []):
        val = (feat.get("properties", {}).get("temperature") or {}).get("value")
        if val is not None and is_precise_celsius(val):
            temps.append(val * 9 / 5 + 32)
    return temps
