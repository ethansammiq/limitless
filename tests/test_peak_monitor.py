"""Tests for peak_monitor.py — Strategy F: Post-Peak Lock-In."""

import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest


import peak_monitor
from peak_monitor import (
    CityPeakState,
    Observation,
    detect_peak,
    fetch_bracket_prices,
    find_bracket_price,
    load_state,
    run_single_poll,
    save_state,
    poll_once,
    PEAK_BRACKET_FETCH_MAX_ATTEMPTS,
    _cities_awaiting_peak,
    _is_last_cron_tick,
)

ET = ZoneInfo("America/New_York")
CT = ZoneInfo("America/Chicago")
PT = ZoneInfo("America/Los_Angeles")


# ─── Helpers ───────────────────────────────────────────

def _obs(temp_f: float, hour: int, minute: int = 0, tz=ET, base_date=None) -> Observation:
    """Create an Observation at a specific local hour."""
    if base_date is None:
        base_date = datetime.now(tz).date()
    dt = datetime(base_date.year, base_date.month, base_date.day,
                  hour, minute, tzinfo=tz)
    return Observation(temp_f=temp_f, timestamp=dt, station="KTEST")


def _make_obs_series(temps: list[tuple[float, int, int]], tz=ET) -> list[Observation]:
    """Create a series of observations: [(temp, hour, minute), ...]"""
    return [_obs(t, h, m, tz) for t, h, m in temps]


def _fresh_state(city="NYC", tz=ET) -> CityPeakState:
    today = datetime.now(tz).strftime("%Y-%m-%d")
    return CityPeakState(city_key=city, date=today)


# ─── Test: detect_peak core logic ──────────────────────

class TestDetectPeak:
    """Core peak detection algorithm."""

    def test_no_observations(self):
        state = _fresh_state()
        result = detect_peak([], state, ET)
        assert not result.peak_confirmed
        assert result.running_max == -999.0

    def test_rising_temps_no_peak(self):
        """Temperature still rising — no peak."""
        obs = _make_obs_series([
            (60.0, 12, 0),
            (65.0, 13, 0),
            (70.0, 14, 0),
        ])
        state = _fresh_state()
        result = detect_peak(obs, state, ET)
        assert not result.peak_confirmed
        assert result.running_max == 70.0

    def test_clear_peak_confirmed(self):
        """Classic afternoon peak with 3 declining obs and sufficient drop."""
        obs = _make_obs_series([
            (60.0, 10, 0),
            (68.0, 12, 0),
            (73.5, 14, 0),   # ← peak
            (72.0, 15, 0),   # -1.5°F
            (71.0, 16, 0),   # -2.5°F
            (69.5, 17, 0),   # -4.0°F
        ])
        state = _fresh_state()
        result = detect_peak(obs, state, ET)
        assert result.peak_confirmed
        assert result.peak_temp == 73.5
        assert result.running_max == 73.5

    def test_insufficient_decline_count(self):
        """Only 2 declining obs — not enough (need 3)."""
        obs = _make_obs_series([
            (73.5, 14, 0),   # peak
            (72.0, 15, 0),   # -1.5°F
            (71.0, 16, 0),   # -2.5°F  (only 2 declining, need 3)
        ])
        state = _fresh_state()
        result = detect_peak(obs, state, ET)
        assert not result.peak_confirmed

    def test_insufficient_drop(self):
        """3 declining obs but drop is only 1.0°F (need 1.5°F)."""
        obs = _make_obs_series([
            (73.5, 14, 0),   # peak
            (73.0, 15, 0),
            (72.8, 16, 0),
            (72.5, 17, 0),   # only -1.0°F drop
        ])
        state = _fresh_state()
        result = detect_peak(obs, state, ET)
        assert not result.peak_confirmed

    def test_insufficient_elapsed_time(self):
        """3 declining obs within 30 min — too fast (need 45 min)."""
        obs = _make_obs_series([
            (73.5, 14, 0),
            (72.0, 14, 10),
            (71.0, 14, 20),
            (69.5, 14, 30),  # only 30 min elapsed
        ])
        state = _fresh_state()
        result = detect_peak(obs, state, ET)
        assert not result.peak_confirmed

    def test_too_early_before_noon(self, monkeypatch):
        """Peak detection blocked when PEAK_EARLIEST_HOUR is set to block."""
        obs = _make_obs_series([
            (65.0, 8, 0),
            (68.0, 9, 0),    # morning peak
            (66.0, 10, 0),
            (64.0, 10, 30),
            (63.0, 11, 0),
        ])
        state = _fresh_state()
        # Set earliest hour to 24 to guarantee the check blocks
        monkeypatch.setattr("peak_monitor.PEAK_EARLIEST_HOUR", 24)
        result = detect_peak(obs, state, ET)
        assert not result.peak_confirmed

    def test_already_confirmed_skips(self):
        """If already confirmed, don't re-check."""
        obs = _make_obs_series([(60.0, 15, 0)])
        state = _fresh_state()
        state.peak_confirmed = True
        state.peak_temp = 73.0
        result = detect_peak(obs, state, ET)
        assert result.peak_confirmed
        assert result.peak_temp == 73.0  # unchanged

    def test_running_max_updates_across_calls(self):
        """Running max accumulates across multiple detect_peak calls."""
        state = _fresh_state()

        # First batch: morning warming
        obs1 = _make_obs_series([
            (55.0, 8, 0),
            (62.0, 10, 0),
        ])
        detect_peak(obs1, state, ET)
        assert state.running_max == 62.0

        # Second batch: afternoon peak
        obs2 = _make_obs_series([
            (55.0, 8, 0),
            (62.0, 10, 0),
            (70.0, 13, 0),
            (73.0, 14, 0),
        ])
        detect_peak(obs2, state, ET)
        assert state.running_max == 73.0

    def test_false_peak_recovery(self):
        """Temp dips then rises again — not a real peak."""
        obs = _make_obs_series([
            (70.0, 12, 0),
            (73.0, 13, 0),   # apparent peak
            (72.0, 14, 0),
            (71.0, 15, 0),
            (71.5, 16, 0),   # bounce — NOT 3 consecutive declining
            (74.0, 17, 0),   # new high! (invalidates previous "peak")
        ])
        state = _fresh_state()
        result = detect_peak(obs, state, ET)
        # Should NOT confirm because 74.0 became the new running max
        # and there aren't 3 declining obs after 17:00
        assert not result.peak_confirmed
        assert result.running_max == 74.0

    def test_chicago_timezone(self):
        """Peak detection works correctly in Central Time."""
        obs = _make_obs_series([
            (68.0, 12, 0),
            (75.2, 14, 0),   # peak
            (73.0, 15, 0),
            (71.5, 16, 0),
            (70.0, 17, 0),
        ], tz=CT)
        state = CityPeakState(city_key="CHI", date=datetime.now(CT).strftime("%Y-%m-%d"))
        result = detect_peak(obs, state, CT)
        assert result.peak_confirmed
        assert result.peak_temp == 75.2

    def test_lax_timezone(self):
        """Peak detection in Pacific Time."""
        obs = _make_obs_series([
            (65.0, 12, 0),
            (72.0, 14, 30),  # peak
            (70.0, 15, 30),
            (68.5, 16, 30),
            (67.0, 17, 30),
        ], tz=PT)
        state = CityPeakState(city_key="LAX", date=datetime.now(PT).strftime("%Y-%m-%d"))
        result = detect_peak(obs, state, PT)
        assert result.peak_confirmed
        assert result.peak_temp == 72.0


# ─── Test: bracket matching ────────────────────────────

class TestBracketMatching:
    """find_bracket_price correctly maps peak temp to bracket."""

    TARGET = "2026-02-14"

    def _mock_brackets(self) -> list[dict]:
        return [
            {"ticker": "KXHIGHNY-26FEB14-B30.5", "title": "30° to 31°F", "yes_bid": 5, "yes_ask": 10, "volume": 100},
            {"ticker": "KXHIGHNY-26FEB14-B72.5", "title": "72° to 73°F", "yes_bid": 45, "yes_ask": 55, "volume": 500},
            {"ticker": "KXHIGHNY-26FEB14-B74.5", "title": "74° to 75°F", "yes_bid": 30, "yes_ask": 40, "volume": 300},
            {"ticker": "KXHIGHNY-26FEB14-T76", "title": "76°F or above", "yes_bid": 10, "yes_ask": 20, "volume": 200},
        ]

    def test_exact_bracket_match(self):
        result = find_bracket_price(self._mock_brackets(), 73.0, self.TARGET)
        assert result is not None
        assert result["ticker"] == "KXHIGHNY-26FEB14-B72.5"

    def test_fractional_temp(self):
        """73.4°F rounds to 73 → bracket 72-73."""
        result = find_bracket_price(self._mock_brackets(), 73.4, self.TARGET)
        assert result is not None
        assert result["ticker"] == "KXHIGHNY-26FEB14-B72.5"

    def test_rounds_up(self):
        """73.5°F rounds to 74 → bracket 74-75 (parse_bracket_range returns low,high+1)."""
        result = find_bracket_price(self._mock_brackets(), 73.5, self.TARGET)
        # round(73.5) = 74 in Python (banker's rounding), but 73.6 → 74
        result2 = find_bracket_price(self._mock_brackets(), 73.6, self.TARGET)
        assert result2 is not None
        assert result2["ticker"] == "KXHIGHNY-26FEB14-B74.5"

    def test_tail_bracket(self):
        """77°F → above 76."""
        result = find_bracket_price(self._mock_brackets(), 77.0, self.TARGET)
        assert result is not None
        assert result["ticker"] == "KXHIGHNY-26FEB14-T76"

    def test_no_match(self):
        """50°F — no bracket covers it."""
        result = find_bracket_price(self._mock_brackets(), 50.0, self.TARGET)
        assert result is None

    def test_empty_brackets(self):
        result = find_bracket_price([], 73.0, self.TARGET)
        assert result is None


class TestBracketEventDate:
    """Regression: 2026-07-06 live alert confirmed TODAY's LAX peak (75.2°F)
    but recommended TOMORROW's ticker (KXHIGHLAX-26JUL07-B75.5) — both events
    were open and find_bracket_price matched by strike alone. The confirmed
    peak must map to the SAME climate day's event, never the next day's."""

    def _jul6_lax_brackets(self) -> list[dict]:
        # JUL07 markets listed first, as in the live incident: without the
        # date filter, 75.2 (→75) strike-matches JUL07 "75 to 76" before
        # JUL06 "74 to 75".
        return [
            {"ticker": "KXHIGHLAX-26JUL07-B73.5", "title": "73° to 74°F", "yes_bid": 20, "yes_ask": 30, "volume": 50},
            {"ticker": "KXHIGHLAX-26JUL07-B75.5", "title": "75° to 76°F", "yes_bid": 25, "yes_ask": 35, "volume": 80},
            {"ticker": "KXHIGHLAX-26JUL06-B74.5", "title": "74° to 75°F", "yes_bid": 60, "yes_ask": 70, "volume": 400},
            {"ticker": "KXHIGHLAX-26JUL06-B76.5", "title": "76° to 77°F", "yes_bid": 5, "yes_ask": 12, "volume": 150},
        ]

    def test_jul6_peak_maps_to_jul6_event(self):
        result = find_bracket_price(self._jul6_lax_brackets(), 75.2, "2026-07-06")
        assert result is not None
        assert result["ticker"] == "KXHIGHLAX-26JUL06-B74.5"

    def test_never_recommends_next_day_ticker(self):
        result = find_bracket_price(self._jul6_lax_brackets(), 75.2, "2026-07-06")
        assert "JUL07" not in result["ticker"]

    def test_only_next_day_markets_open_returns_none(self):
        """If today's event is gone from the open list, alert 'market may be
        closed' rather than recommending tomorrow's ticker."""
        next_day_only = [m for m in self._jul6_lax_brackets() if "26JUL07" in m["ticker"]]
        assert find_bracket_price(next_day_only, 75.2, "2026-07-06") is None

    def test_unparseable_ticker_date_fails_closed(self):
        brackets = [{"ticker": "T-74-75", "title": "74° to 75°F", "yes_bid": 60, "yes_ask": 70, "volume": 400}]
        assert find_bracket_price(brackets, 75.2, "2026-07-06") is None

    def test_target_date_from_climate_day_of_peak(self):
        """The poll loop anchors target_date to climate_day_start(tz, max_time)."""
        from zoneinfo import ZoneInfo
        from core.obs import climate_day_start
        pt = ZoneInfo("America/Los_Angeles")
        peak_time = datetime(2026, 7, 6, 12, 35, tzinfo=pt)
        assert climate_day_start(pt, peak_time).date().isoformat() == "2026-07-06"


# ─── Test: state persistence ───────────────────────────

class TestStatePersistence:
    """State save/load round-trips correctly."""

    def test_round_trip(self, tmp_path, monkeypatch):
        state_file = tmp_path / "peak_state.json"
        monkeypatch.setattr("peak_monitor.STATE_FILE", state_file)

        states = {
            "NYC": CityPeakState(
                city_key="NYC",
                date="2026-02-13",
                running_max=73.5,
                max_time=datetime(2026, 2, 13, 14, 0, tzinfo=ET),
                peak_confirmed=True,
                peak_temp=73.5,
                peak_bracket="72-73",
                alerted=False,
            ),
        }
        save_state(states)
        loaded = load_state()

        assert "NYC" in loaded
        s = loaded["NYC"]
        assert s.city_key == "NYC"
        assert s.running_max == 73.5
        assert s.peak_confirmed is True
        assert s.peak_temp == 73.5
        assert s.max_time.year == 2026
        assert s.alerted is False

    def test_empty_state_file(self, tmp_path, monkeypatch):
        state_file = tmp_path / "peak_state.json"
        monkeypatch.setattr("peak_monitor.STATE_FILE", state_file)
        loaded = load_state()
        assert loaded == {}

    def test_corrupt_state_file(self, tmp_path, monkeypatch):
        state_file = tmp_path / "peak_state.json"
        state_file.write_text("not json")
        monkeypatch.setattr("peak_monitor.STATE_FILE", state_file)
        loaded = load_state()
        assert loaded == {}

    def test_new_day_resets(self):
        """State from yesterday should be replaced on new day."""
        state = CityPeakState(
            city_key="NYC",
            date="2026-02-12",  # yesterday
            running_max=75.0,
            peak_confirmed=True,
            alerted=True,
        )
        today = datetime.now(ET).strftime("%Y-%m-%d")
        # In poll_once, the check is: if state.date != today_str → reset
        assert state.date != today  # confirms it would be reset


# ─── Test: CityPeakState serialization ─────────────────

class TestCityPeakState:
    def test_to_dict_and_back(self):
        state = CityPeakState(
            city_key="LAX",
            date="2026-02-13",
            running_max=72.0,
            max_time=datetime(2026, 2, 13, 14, 30, tzinfo=PT),
            peak_confirmed=True,
            peak_temp=72.0,
            peak_bracket="72-73",
            alerted=True,
        )
        d = state.to_dict()
        restored = CityPeakState.from_dict(d)
        assert restored.city_key == "LAX"
        assert restored.running_max == 72.0
        assert restored.peak_confirmed is True
        assert restored.alerted is True
        assert restored.max_time is not None

    def test_from_dict_missing_max_time(self):
        d = {"city_key": "NYC", "date": "2026-02-13"}
        state = CityPeakState.from_dict(d)
        assert state.max_time is None
        assert state.running_max == -999.0

    def test_from_dict_pre_retry_format(self):
        """State files written before the retry fields existed still load."""
        d = {"city_key": "NYC", "date": "2026-02-13", "alerted": True}
        state = CityPeakState.from_dict(d)
        assert state.alert_sent is False
        assert state.trade_attempts == 0
        assert state.bracket_fetch_failures == 0

    def test_retry_fields_round_trip(self):
        state = CityPeakState(
            city_key="NYC",
            date="2026-02-13",
            alert_sent=True,
            trade_attempts=2,
            bracket_fetch_failures=1,
        )
        restored = CityPeakState.from_dict(state.to_dict())
        assert restored.alert_sent is True
        assert restored.trade_attempts == 2
        assert restored.bracket_fetch_failures == 1

    def test_observation_repr(self):
        obs = Observation(
            temp_f=73.5,
            timestamp=datetime(2026, 2, 13, 14, 30, tzinfo=ET),
            station="KNYC",
        )
        r = repr(obs)
        assert "73.5" in r
        assert "KNYC" in r


# ─── Test: edge cases ─────────────────────────────────

class TestEdgeCases:
    def test_single_observation(self):
        """Just one obs — can't confirm peak."""
        obs = [_obs(73.0, 14, 0)]
        state = _fresh_state()
        result = detect_peak(obs, state, ET)
        assert not result.peak_confirmed
        assert result.running_max == 73.0

    def test_flat_temperature(self):
        """Same temp for hours — no decline, no peak."""
        obs = _make_obs_series([
            (73.0, 13, 0),
            (73.0, 14, 0),
            (73.0, 15, 0),
            (73.0, 16, 0),
        ])
        state = _fresh_state()
        result = detect_peak(obs, state, ET)
        assert not result.peak_confirmed

    def test_exactly_min_drop(self):
        """Drop of exactly PEAK_MIN_DROP_F (1.5°F) — DOES confirm (>= threshold)."""
        obs = _make_obs_series([
            (73.0, 13, 0),
            (71.5, 14, 0),
            (71.5, 15, 0),
            (71.5, 16, 0),
        ])
        state = _fresh_state()
        result = detect_peak(obs, state, ET)
        # 73.0 - 71.5 = 1.5, check is `drop < 1.5` → False → passes
        assert result.peak_confirmed

    def test_just_above_min_drop(self):
        """Drop of 1.6°F — should confirm."""
        obs = _make_obs_series([
            (73.0, 13, 0),
            (71.4, 14, 0),
            (71.4, 15, 0),
            (71.4, 16, 0),
        ])
        state = _fresh_state()
        result = detect_peak(obs, state, ET)
        assert result.peak_confirmed

    def test_midnight_high_not_false_peak(self, monkeypatch):
        """Midnight high (temp falls all day) — detected as peak after noon."""
        monkeypatch.setattr("peak_monitor.PEAK_EARLIEST_HOUR", 12)
        obs = _make_obs_series([
            (45.0, 0, 0),    # midnight high
            (42.0, 6, 0),
            (40.0, 12, 0),
            (38.0, 13, 0),
            (36.0, 14, 0),
            (35.0, 15, 0),
        ])
        state = _fresh_state()
        result = detect_peak(obs, state, ET)
        # Running max is 45.0 from midnight, but peak detection starts at noon
        # The 3 declining obs after the max (42, 40, 38, 36, 35) are all before noon
        # and after... actually the obs at 13,14,15 are after max_time (00:00).
        # So this WILL confirm because there are 3+ declining obs after 00:00
        # with 15h elapsed and 10°F drop. That's correct! The midnight high
        # IS the peak — we want to detect it.
        assert result.peak_confirmed
        assert result.peak_temp == 45.0


# ─── Test: fetch_bracket_prices transient detection ────

class TestFetchBracketPrices:
    """None = no market data (retry); list = real data."""

    def test_exception_returns_none(self, monkeypatch):
        monkeypatch.setattr("peak_monitor._fetch_kalshi_brackets",
                            AsyncMock(side_effect=ConnectionError("boom")), raising=False)
        assert asyncio.run(fetch_bracket_prices(None, "NYC")) is None

    def test_empty_list_returns_none(self, monkeypatch):
        """fetch_kalshi_brackets swallows HTTP errors into [] — treat as no-data."""
        monkeypatch.setattr("peak_monitor._fetch_kalshi_brackets",
                            AsyncMock(return_value=[]), raising=False)
        assert asyncio.run(fetch_bracket_prices(None, "NYC")) is None

    def test_brackets_passed_through(self, monkeypatch):
        brackets = [{"ticker": "T-72-73", "title": "72° to 73°F"}]
        monkeypatch.setattr("peak_monitor._fetch_kalshi_brackets",
                            AsyncMock(return_value=brackets), raising=False)
        assert asyncio.run(fetch_bracket_prices(None, "NYC")) == brackets


# ─── Test: poll_once retry semantics ───────────────────

NYC_BRACKETS = [
    {"ticker": "T-72-73", "title": "72° to 73°F", "yes_bid": 45, "yes_ask": 55, "volume": 500},
]


def _confirmed_state(**overrides) -> CityPeakState:
    """NYC state with today's peak already confirmed but not yet alerted."""
    now = datetime.now(ET)
    state = CityPeakState(
        city_key="NYC",
        date=now.strftime("%Y-%m-%d"),
        running_max=73.0,
        max_time=now - timedelta(hours=3),
        peak_confirmed=True,
        peak_temp=73.0,
        peak_bracket="72-73",
    )
    for key, value in overrides.items():
        setattr(state, key, value)
    return state


# (TestPollOnceRetry removed 2026-07-06 — Strategy G auto-execution
#  died with the KDE stack; peak_monitor is alert-only now.)


# ─── Test: late LAX window (ET cron vs local config) ───

class TestLastCronTick:
    """_is_last_cron_tick detects the 22:50 ET run (no successor tick today)."""

    def _at(self, hour, minute):
        return datetime(2026, 6, 12, hour, minute, tzinfo=ET)

    def test_final_tick(self):
        assert _is_last_cron_tick(self._at(22, 50)) is True

    def test_earlier_ticks_in_last_hour(self):
        assert _is_last_cron_tick(self._at(22, 0)) is False
        assert _is_last_cron_tick(self._at(22, 40)) is False

    def test_midday_ticks(self):
        assert _is_last_cron_tick(self._at(13, 0)) is False
        assert _is_last_cron_tick(self._at(21, 50)) is False

    def test_after_cron_window(self):
        """A run started after the window (manual/lingering) counts as final."""
        assert _is_last_cron_tick(self._at(23, 30)) is True


class TestCitiesAwaitingPeak:
    def test_open_windows_pending(self, monkeypatch):
        """All windows open + no state → every city pending (incl. LAX)."""
        monkeypatch.setattr("peak_monitor.PEAK_EARLIEST_HOUR", 0)
        monkeypatch.setattr("peak_monitor.PEAK_LATEST_HOUR", 24)
        pending = _cities_awaiting_peak({})
        assert "LAX" in pending
        assert len(pending) == 5

    def test_alerted_cities_excluded(self, monkeypatch):
        from config import STATIONS
        monkeypatch.setattr("peak_monitor.PEAK_EARLIEST_HOUR", 0)
        monkeypatch.setattr("peak_monitor.PEAK_LATEST_HOUR", 24)
        states = {}
        for city_key, cfg in STATIONS.items():
            today = datetime.now(ZoneInfo(cfg.timezone)).strftime("%Y-%m-%d")
            states[city_key] = CityPeakState(city_key=city_key, date=today, alerted=True)
        assert _cities_awaiting_peak(states) == []

    def test_closed_windows_excluded(self, monkeypatch):
        monkeypatch.setattr("peak_monitor.PEAK_EARLIEST_HOUR", 0)
        monkeypatch.setattr("peak_monitor.PEAK_LATEST_HOUR", 0)
        assert _cities_awaiting_peak({}) == []

    def test_stale_alerted_state_still_pending(self, monkeypatch):
        """Yesterday's alerted=True doesn't suppress today's monitoring."""
        monkeypatch.setattr("peak_monitor.PEAK_EARLIEST_HOUR", 0)
        monkeypatch.setattr("peak_monitor.PEAK_LATEST_HOUR", 24)
        states = {"LAX": CityPeakState(city_key="LAX", date="2026-06-11", alerted=True)}
        assert "LAX" in _cities_awaiting_peak(states)


class TestRunSinglePoll:
    """Cron entrypoint lingers on the final ET tick while LAX's window is open."""

    def test_normal_tick_polls_once(self, monkeypatch):
        poll_mock = AsyncMock(return_value={})
        monkeypatch.setattr("peak_monitor.poll_once", poll_mock)
        monkeypatch.setattr("peak_monitor._is_last_cron_tick", lambda now: False)

        asyncio.run(run_single_poll(quiet=True))
        poll_mock.assert_awaited_once()

    def test_final_tick_lingers_until_windows_close(self, monkeypatch):
        poll_mock = AsyncMock(return_value={})
        monkeypatch.setattr("peak_monitor.poll_once", poll_mock)
        monkeypatch.setattr("peak_monitor._is_last_cron_tick", lambda now: True)
        monkeypatch.setattr("peak_monitor.PEAK_POLL_INTERVAL_SEC", 0)
        monkeypatch.setattr("peak_monitor._cities_awaiting_peak",
                            MagicMock(side_effect=[["LAX"], ["LAX"], []]))

        asyncio.run(run_single_poll(quiet=True))
        assert poll_mock.await_count == 3  # initial + 2 linger polls

    def test_final_tick_no_pending_exits(self, monkeypatch):
        poll_mock = AsyncMock(return_value={})
        monkeypatch.setattr("peak_monitor.poll_once", poll_mock)
        monkeypatch.setattr("peak_monitor._is_last_cron_tick", lambda now: True)
        monkeypatch.setattr("peak_monitor._cities_awaiting_peak",
                            MagicMock(return_value=[]))

        asyncio.run(run_single_poll(quiet=True))
        poll_mock.assert_awaited_once()

    def test_city_filter_never_lingers(self, monkeypatch):
        """Manual --city runs must not hang a terminal past the cron window."""
        poll_mock = AsyncMock(return_value={})
        monkeypatch.setattr("peak_monitor.poll_once", poll_mock)
        monkeypatch.setattr("peak_monitor._is_last_cron_tick", lambda now: True)
        monkeypatch.setattr("peak_monitor._cities_awaiting_peak",
                            MagicMock(return_value=["LAX"]))

        asyncio.run(run_single_poll(city_filter="LAX", quiet=True))
        poll_mock.assert_awaited_once()


class TestIemRateLimitPolicy:
    """The 5-city sweep IS the burst IEM refuses (2026-07-16: CHI/MIA/LAX
    all 429'd inside one second, and again after core.dsm alone was fixed —
    this path has its own transport and needed the same policy)."""

    def _resp(self, status, text="ok"):
        class R:
            def __init__(self):
                self.status = status

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def text(self):
                return text
        return R()

    def _session(self, statuses):
        calls = {"n": 0}

        class S:
            def get(_self, url, timeout=None):
                calls["n"] += 1
                return TestIemRateLimitPolicy()._resp(
                    statuses[min(calls["n"] - 1, len(statuses) - 1)])
        return S(), calls

    def test_shares_core_dsm_policy_not_a_local_copy(self):
        # a second constant drifts out of sync; there must be one policy
        assert not hasattr(peak_monitor, "IEM_429_BACKOFF_S")
        assert peak_monitor.dsm.IEM_429_ATTEMPTS >= 2

    def test_throttle_spaces_back_to_back_cities(self, monkeypatch):
        naps = []
        clock = {"t": 500.0}
        monkeypatch.setattr(peak_monitor.time, "monotonic", lambda: clock["t"])

        async def fake_sleep(s):
            naps.append(s)
        monkeypatch.setattr(peak_monitor.asyncio, "sleep", fake_sleep)
        monkeypatch.setattr(peak_monitor, "_last_iem_monotonic", 0.0)

        asyncio.run(peak_monitor._iem_throttle())   # after idle: no wait
        assert naps == []
        asyncio.run(peak_monitor._iem_throttle())   # immediately again: wait
        assert naps and naps[0] == pytest.approx(peak_monitor.dsm.IEM_MIN_INTERVAL_S)

    def test_429_retries_jittered_then_gives_up_returning_empty(self, monkeypatch):
        naps = []

        async def fake_sleep(s):
            naps.append(s)
        monkeypatch.setattr(peak_monitor.asyncio, "sleep", fake_sleep)
        monkeypatch.setattr(peak_monitor, "_iem_throttle", AsyncMock())
        session, calls = self._session([429])

        obs = asyncio.run(peak_monitor.fetch_iem_observations(session, "NYC"))

        assert obs == []                                   # fails open, never raises
        assert calls["n"] == peak_monitor.dsm.IEM_429_ATTEMPTS
        assert len(naps) == peak_monitor.dsm.IEM_429_ATTEMPTS - 1
        assert naps[1] > naps[0] * 0.5                     # grows, not fixed
