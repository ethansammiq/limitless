"""METAR 6-hourly climate groups — an earlier settlement leak than the CLI.

2026-07-11 live: the KMSP 112353Z METAR carried remark group `10322`
(6-hr max 32.2°C = 89.96°F → CLI printed 90) and the 99¢×119k certainty
wall repriced KXHIGHTMIN-26JUL11-B88.5 immediately after that METAR — the
walls read these groups. The `1sTTT` (6-hr max) and `2sTTT` (6-hr min)
remark groups appear in the synoptic-window METARs (~05/11/17/23Z, i.e.
the :53 obs before 00/06/12/18Z) and carry the settlement-precise extreme
in tenths of °C, hours before the CLI report Kalshi settles on.

Semantics mirror the CLI floor logic: a 6-hr max M is a FLOOR on the day's
high (final ≥ M); a 6-hr min m is a CEILING on the day's low (final ≤ m).
°F rounding must use the precise tenths value: 32.2°C = 89.96°F rounds to
90, while int(°C→°F) truncation would say 89 — a full bracket wrong.

Feed: aviationweather.gov/api/data/metar (raw format, one request for ALL
ladder stations via comma-joined ids). Fail open on any fetch error — the
detector only ever suggests, and every alert is human-verified.
"""
from __future__ import annotations

import math
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

METAR_URL = "https://aviationweather.gov/api/data/metar?{query}"
USER_AGENT = "WeatherEdgeMETAR/1.0"

# "KMSP 112353Z ..." — station + ddhhmmZ obs time at the head of a raw METAR.
_HEAD = re.compile(r"^(?:METAR\s+|SPECI\s+)?([A-Z][A-Z0-9]{3})\s+(\d{2})(\d{2})(\d{2})Z\b")
# 6-hourly temp groups live in the RMK section: 1sTTT (max) / 2sTTT (min),
# s=0 positive, s=1 negative, TTT in tenths of °C. Exactly 5 standalone
# digits — pressure-tendency (5appp), precip (6RRRR/7RRRR) and the T-group
# fail the leading [12][01] shape; PK WND dddff/hhmm (which CAN look like
# 2sTTT) is stripped before matching and slash/decimal-adjacent tokens
# are rejected outright.
_PK_WND = re.compile(r"PK\s+WND\s+\d{5,6}/\d{2,4}")
_SIX_HR = re.compile(r"(?<![\d/.])([12])([01])(\d{3})(?![\d/.])")


@dataclass(frozen=True)
class SixHrExtreme:
    station: str            # ICAO, e.g. KMSP
    obs_time_utc: datetime
    kind: str               # "max" | "min"
    tenths_c: int           # signed tenths of °C (322 → 32.2°C)

    @property
    def temp_c(self) -> float:
        return self.tenths_c / 10.0

    @property
    def temp_f(self) -> float:
        """Precise °F from the tenths-°C value — never pre-rounded."""
        return self.temp_c * 9 / 5 + 32

    @property
    def temp_f_rounded(self) -> int:
        return round_f(self.temp_f)


def round_f(f: float) -> int:
    """NWS climate rounding: nearest integer °F, halves up (89.96 → 90).

    math.floor(x + 0.5), not round() — banker's rounding would send 88.5
    to 88 while the CLI prints 89.
    """
    return math.floor(f + 0.5)


def synoptic_anchor_utc(obs_time_utc: datetime) -> int:
    """Nearest synoptic hour (0/6/12/18 UTC) for a group-bearing ob.

    The groups ride the :53 ob BEFORE the synoptic hour (2353Z → 0), with
    stragglers and corrections trailing up to ~45 min after it — both must
    resolve to the same anchor. The 00Z anchor is the only one at which all
    four of a climate day's groups exist (the day-max == final 98.4% class);
    earlier anchors carry post-window warming risk (2026-07-13: the 18Z
    batch would have gone 1-for-5 against the finals).
    """
    return int(6 * round((obs_time_utc.hour + obs_time_utc.minute / 60) / 6)) % 24


def metar_time_to_utc(day: int, hour: int, minute: int,
                      now_utc: datetime) -> datetime | None:
    """Resolve a METAR ddhhmmZ stamp against now (month/year not encoded).

    Tries the current month then the previous one; the stamp must land in
    the past-but-recent window (≤ 36h old) or it's stale/garbled → None.
    """
    for months_back in (0, 1):
        anchor = (now_utc.replace(day=1) - timedelta(days=1)).replace(day=1) \
            if months_back else now_utc
        try:
            t = anchor.replace(day=day, hour=hour, minute=minute,
                               second=0, microsecond=0)
        except ValueError:
            continue
        age = now_utc - t
        if timedelta(minutes=-10) <= age <= timedelta(hours=36):
            return t
    return None


def parse_metars(raw: str, now_utc: datetime) -> list[SixHrExtreme]:
    """All 6-hourly extremes in a raw METAR blob (one report per line)."""
    out: list[SixHrExtreme] = []
    for line in (raw or "").splitlines():
        head = _HEAD.match(line.strip())
        if not head:
            continue
        _, sep, remarks = line.partition("RMK")
        if not sep:
            continue
        obs = metar_time_to_utc(int(head.group(2)), int(head.group(3)),
                                int(head.group(4)), now_utc)
        if obs is None:
            continue
        for m in _SIX_HR.finditer(_PK_WND.sub(" ", remarks)):
            tenths = int(m.group(3)) * (-1 if m.group(2) == "1" else 1)
            out.append(SixHrExtreme(
                station=head.group(1),
                obs_time_utc=obs,
                kind="max" if m.group(1) == "1" else "min",
                tenths_c=tenths,
            ))
    return out


def climate_date(extreme: SixHrExtreme, tz: str) -> str | None:
    """ISO local date the 6-hr window [obs-6h, obs] covers, or None.

    A window straddling local midnight is ambiguous — the extreme could
    belong to either climate day — so it never classifies (skip, journal).
    """
    zone = ZoneInfo(tz)
    end = extreme.obs_time_utc.astimezone(zone)
    start = (extreme.obs_time_utc - timedelta(hours=6)).astimezone(zone)
    if start.date() != end.date():
        return None
    return end.date().isoformat()


def fetch_metars(icaos: list[str], hours: int = 3, timeout: int = 30) -> str:
    """Raw METAR text for many stations in ONE request. Raises on failure —
    the caller decides (detector fails open: log, heartbeat, retry next cron)."""
    query = urllib.parse.urlencode({
        "ids": ",".join(sorted(icaos)), "format": "raw",
        "hours": str(hours), "taf": "false",
    })
    req = urllib.request.Request(METAR_URL.format(query=query),
                                 headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")
