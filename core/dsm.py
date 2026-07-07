"""ASOS Daily Summary Message (DSM) — the settlement oracle for CLI floors.

2026-07-06 MIA: the prelim CLI printed a max of 92 while the station's DSM
had 93/1344 committed hours earlier; the final CLI followed the DSM. An IEM
AFOS archive study (85 days at MIA) found final CLI max == DSM max 85/85,
including all 3 prelim-vs-DSM conflicts. The DSM is therefore treated as
authoritative over any same-day CLI print: an extreme in the DSM that beats
a CLI floor kills any trade premised on the printed value.

Feed: IEM AFOS archive (pil=DSM{awips}). Aggressively rate-limited upstream,
so callers fetch at most once per station per product. Fetch failures return
[] and the caller proceeds unchecked — the veto only ever REMOVES alert
suggestions, and every alert is human-verified before trading.

Product format (live samples 2026-07-07, KMIA):
    KMIA DS 06/07 931344/ 771818// 93/ 78//0010422/...      (daily)
    KMIA DS 0200 07/07 810159/ 740050// 93/ 78//...          (intraday)
The date group is DD/MM. The first two fields are max and min as TEMPHHMM
tokens — the trailing 4 digits are the LST time, the prefix the integer °F
(possibly negative or 3-digit); 'M' marks a missing value. Intraday
snapshots are usable as-is: a so-far max only rises and a so-far min only
falls, so either already contradicting a CLI floor is decisive.
"""
from __future__ import annotations

import re
import urllib.request
from dataclasses import dataclass

DSM_URL = ("https://mesonet.agron.iastate.edu/cgi-bin/afos/retrieve.py"
           "?pil=DSM{awips}&fmt=text&limit=6")
USER_AGENT = "WeatherEdgeDSM/1.0"

_REPORT = re.compile(
    r"^\s*K?(\w{3})\s+DS\s+(?:\d{4}\s+)?(\d{2})/(\d{2})\s+"
    r"(?:(-?\d+)(\d{4})|M)/\s*(?:(-?\d+)(\d{4})|M)//",
    re.M)


@dataclass(frozen=True)
class DSMReport:
    awips: str
    day: int
    month: int
    max_f: int | None
    max_time_lst: str | None   # HHMM, station local STANDARD time
    min_f: int | None
    min_time_lst: str | None


def parse_dsm_text(text: str) -> list[DSMReport]:
    """All parseable DSM reports in an AFOS text blob, newest first."""
    out = []
    for m in _REPORT.finditer(text or ""):
        out.append(DSMReport(
            awips=m.group(1).upper(),
            day=int(m.group(2)),
            month=int(m.group(3)),
            max_f=int(m.group(4)) if m.group(4) else None,
            max_time_lst=m.group(5),
            min_f=int(m.group(6)) if m.group(6) else None,
            min_time_lst=m.group(7),
        ))
    return out


def fetch_dsm_reports(awips: str, timeout: int = 20) -> list[DSMReport]:
    """Recent DSM reports for a station; [] on ANY failure (fail open)."""
    url = DSM_URL.format(awips=awips.upper())
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 — the veto must never kill the sniper run
        return []
    return parse_dsm_text(text)


def reports_for_date(reports: list[DSMReport], iso_date: str) -> list[DSMReport]:
    """Reports covering the given ISO date (DSM carries only DD/MM)."""
    try:
        _, month, day = (int(p) for p in iso_date.split("-"))
    except ValueError:
        return []
    return [r for r in reports if r.day == day and r.month == month]


def dsm_extreme(reports: list[DSMReport], kind: str) -> tuple[int, str] | None:
    """Most extreme (value, HHMM-LST) across reports for one ladder kind."""
    if kind == "high":
        vals = [(r.max_f, r.max_time_lst) for r in reports if r.max_f is not None]
        return max(vals, key=lambda v: v[0]) if vals else None
    vals = [(r.min_f, r.min_time_lst) for r in reports if r.min_f is not None]
    return min(vals, key=lambda v: v[0]) if vals else None


def contradicts(kind: str, printed: int, extreme_f: int) -> bool:
    """Does the DSM extreme already invalidate a CLI-printed floor value?

    Strict inequality only: an intraday DSM extreme milder than the print is
    just an earlier snapshot, not a conflict.
    """
    return extreme_f > printed if kind == "high" else extreme_f < printed
