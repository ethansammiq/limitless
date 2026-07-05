"""Tests for the ladder registry — code AND the committed artifact."""
import re
from zoneinfo import ZoneInfo

import ladders as lad_mod
from scripts.build_ladder_config import (
    kind_from_series,
    parse_settlement_url,
    station_icao_from_awips,
)


class TestGeneratorHelpers:
    def test_settlement_url_parse(self):
        url = "https://forecast.weather.gov/product.php?site=LOT&product=CLI&issuedby=MDW"
        assert parse_settlement_url(url) == ("LOT", "MDW")

    def test_unparseable_is_none(self):
        assert parse_settlement_url("https://example.com/nope") is None
        assert parse_settlement_url("") is None

    def test_kind_from_series(self):
        assert kind_from_series("KXHIGHCHI") == "high"
        assert kind_from_series("KXLOWTNYC") == "low"

    def test_icao_mapping(self):
        assert station_icao_from_awips("MDW") == "KMDW"
        assert station_icao_from_awips("KJFK") == "KJFK"


class TestCommittedArtifact:
    """Guards ladders.json itself — the file cron jobs will trust."""

    def test_loads_and_validates(self):
        ladders = lad_mod.load_ladders()
        assert len(ladders) >= 40
        assert len({l.series for l in ladders}) == len(ladders)
        for lad in ladders:
            assert lad.kind in ("high", "low"), lad
            assert re.fullmatch(r"K[A-Z0-9]{3}", lad.station_icao), lad
            ZoneInfo(lad.tz)  # raises if invalid

    def test_critical_station_pins(self):
        # The two mappings that have burned us / would burn us:
        by_series = {l.series: l for l in lad_mod.load_ladders()}
        assert by_series["KXHIGHNY"].station_icao == "KNYC"      # Central Park, NEVER KLGA
        assert by_series["KXLOWTMIN"].station_icao == "KMSP"    # MIN ticker != MIN station

    def test_high_low_share_stations(self):
        groups = lad_mod.by_awips()
        for awips, group in groups.items():
            assert len({l.station_icao for l in group}) == 1, awips
            assert len({l.tz for l in group}) == 1, awips

    def test_wfos_cover_all(self):
        offices = lad_mod.wfos()
        assert "LOT" in offices and "OKX" in offices
        for tz in offices.values():
            ZoneInfo(tz)
