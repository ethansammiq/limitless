"""Validated Kalshi weather-ladder registry (reads committed ladders.json).

One entry per series with its settlement metadata, generated from Kalshi's
own series API by scripts/build_ladder_config.py and committed after review.
Consumers (cli_sniper, dead_bracket_sweeper) import from here and never
re-derive stations at runtime.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

LADDERS_FILE = Path(__file__).resolve().parent / "ladders.json"


@dataclass(frozen=True)
class Ladder:
    series: str        # KXHIGHCHI
    kind: str          # "high" | "low"
    awips: str         # MDW — station code inside CLI products
    wfo: str           # LOT — NWS office that issues the CLI
    station_icao: str  # KMDW — NWS observations station
    tz: str            # IANA timezone


def load_ladders(path: Path = LADDERS_FILE) -> list[Ladder]:
    return [Ladder(**e) for e in json.loads(path.read_text())]


def by_awips(ladders: list[Ladder] | None = None) -> dict[str, list[Ladder]]:
    """AWIPS station code -> its ladders (a station's high AND low series)."""
    out: dict[str, list[Ladder]] = {}
    for lad in ladders if ladders is not None else load_ladders():
        out.setdefault(lad.awips, []).append(lad)
    return out


def by_station(ladders: list[Ladder] | None = None) -> dict[str, list[Ladder]]:
    """NWS ICAO station id -> its ladders."""
    out: dict[str, list[Ladder]] = {}
    for lad in ladders if ladders is not None else load_ladders():
        out.setdefault(lad.station_icao, []).append(lad)
    return out


def wfos(ladders: list[Ladder] | None = None) -> dict[str, str]:
    """WFO -> representative tz (offices are single-timezone for our set)."""
    out: dict[str, str] = {}
    for lad in ladders if ladders is not None else load_ladders():
        out.setdefault(lad.wfo, lad.tz)
    return out
