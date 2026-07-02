#!/usr/bin/env python3
"""Tests for proxy_arb_engine.py.

Covers: Kalshi taker fee math, local-climate-day peak windowing, bracket
probability window vs the edge_scanner_v2 convention, trading guards on the
live order path, dry_run defaults, today-only bracket scanning, NO-side
floor-exclusion opportunities, ask-cross fee handling, observation staleness,
and top-of-book size capping.
"""

import asyncio
import inspect
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

import proxy_arb_engine as pae
from proxy_arb_engine import (
    ASOSPeak,
    ProxyArbEngine,
    _parse_bracket,
    _parse_iem_asos_csv,
    _point_prob_in_bracket,
    kalshi_taker_fee_cents,
    run_proxy_scan,
)
from utils.state_db import StateDB

LAX_TZ = ZoneInfo("America/Los_Angeles")
UTC = timezone.utc


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_engine(tmp_path, **kwargs) -> tuple[ProxyArbEngine, MagicMock]:
    db = StateDB(tmp_path / "state.db")
    client = MagicMock()
    client.get_balance = AsyncMock(return_value=5000.0)
    client.place_order = AsyncMock(
        return_value={"order": {"order_id": "ORD-1", "status": "resting"}}
    )
    engine = ProxyArbEngine(
        "LAX", kalshi_client=client, db=db, session=MagicMock(), **kwargs
    )
    return engine, client


def _fresh_peak(peak_f: float = 75.0, current_f: float | None = 75.0,
                obs_age_min: float = 5.0) -> ASOSPeak:
    now = datetime.now(UTC)
    return ASOSPeak(
        station_id="LAX",
        peak_temp_f=peak_f,
        current_temp_f=current_f,
        peak_time_utc=now,
        record_count=10,
        source="iem_metar",
        current_obs_utc=now - timedelta(minutes=obs_age_min),
    )


def _today_ticker(suffix: str = "B76") -> str:
    date_tag = datetime.now(LAX_TZ).strftime("%y%b%d").upper()
    return f"KXHIGHLAX-{date_tag}-{suffix}"


def _wire_signal_engine(engine, monkeypatch, peak: ASOSPeak,
                        guards_result=(True, ["PASS: ok"]),
                        tob_size=None):
    """Stub all I/O so evaluate_and_trade runs offline."""
    engine.fetch_asos_daily_peak = AsyncMock(return_value=peak)
    engine.calculate_propagation_vector = AsyncMock(return_value=[])
    engine._top_of_book_size = AsyncMock(return_value=tob_size)

    guard_mock = MagicMock(return_value=guards_result)
    monkeypatch.setattr(pae, "run_all_pre_trade_checks", guard_mock)
    monkeypatch.setattr(pae, "send_discord_alert", AsyncMock())

    import position_store
    monkeypatch.setattr(position_store, "load_positions", lambda: [])
    monkeypatch.setattr(position_store, "register_position", lambda *a, **k: None)
    return guard_mock


# ── Fee math ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("price_cents,expected_fee", [
    (0, 0),       # degenerate
    (100, 0),     # degenerate
    (1, 1),       # 0.07*1*99/100 = 0.0693 → 1
    (11, 1),      # 0.6853 → 1
    (30, 2),      # 1.47 → 2
    (50, 2),      # 1.75 → 2 (max fee)
    (60, 2),      # 1.68 → 2
    (89, 1),      # 0.6853 → 1
    (99, 1),      # 0.0693 → 1
])
def test_taker_fee_cents(price_cents, expected_fee):
    assert kalshi_taker_fee_cents(price_cents) == expected_fee


def test_taker_fee_clamps_out_of_range_prices():
    assert kalshi_taker_fee_cents(-5) == 0
    assert kalshi_taker_fee_cents(150) == 0


# ── Bracket parsing & probability window ─────────────────────────────────────

@pytest.mark.parametrize("title", [
    "44-45°F",
    "44 to 45",
    "70° to 71°",
    "44° or below",
    "90° or above",
])
def test_parse_bracket_matches_edge_scanner_convention(title):
    from edge_scanner_v2 import parse_bracket_range
    parsed = _parse_bracket(title)
    assert parsed is not None
    assert parsed == parse_bracket_range(title)


def test_parse_bracket_range_window_is_two_degrees():
    # '44-45' settles YES for {44, 45} → window (44, 46), not (44, 45)
    assert _parse_bracket("44-45°F") == (44.0, 46.0, "range")


def test_parse_bracket_unparseable_returns_none():
    assert _parse_bracket("garbage title") is None


def test_prob_window_not_capped_at_quarter():
    # With the old 1°F window, max attainable prob at σ=1.5 was ~0.26.
    # The 2°F window centered on the prediction reaches ~0.495.
    lo, hi, _ = _parse_bracket("44-45°F")
    p = _point_prob_in_bracket(45.0, lo, hi)
    assert 0.45 < p < 0.55


def test_prob_tail_brackets_integrate():
    lo, hi, kind = _parse_bracket("90° or above")
    assert kind == "high_tail"
    assert _point_prob_in_bracket(95.0, lo, hi) > 0.99
    lo, hi, kind = _parse_bracket("44° or below")
    assert kind == "low_tail"
    assert _point_prob_in_bracket(40.0, lo, hi) > 0.99


# ── Local climate day ────────────────────────────────────────────────────────

def test_parse_csv_filters_to_local_climate_day():
    # LAX local day 2026-06-12 → UTC window [2026-06-12 07:00, 2026-06-13 07:00)
    start = datetime(2026, 6, 12, 7, 0, tzinfo=UTC)
    end = start + timedelta(days=1)
    csv_text = (
        "station,valid,tmpf,drct,sknt\n"
        "LAX,2026-06-12 01:00,95.0,270,10\n"  # 2026-06-11 18:00 PDT — yesterday local
        "LAX,2026-06-12 15:00,70.0,250,8\n"   # 2026-06-12 08:00 PDT — today local
        "LAX,2026-06-12 22:00,72.0,240,6\n"   # 2026-06-12 15:00 PDT — today local
    )
    parsed = _parse_iem_asos_csv(csv_text, "LAX", start, end)
    assert parsed.record_count == 2
    assert parsed.peak_temp_f == 72.0  # yesterday's 95°F afternoon high excluded
    assert parsed.peak_time_utc == datetime(2026, 6, 12, 22, 0, tzinfo=UTC)
    assert parsed.current_temp_f == 72.0
    assert parsed.last_obs_utc == datetime(2026, 6, 12, 22, 0, tzinfo=UTC)


def test_parse_csv_excludes_next_local_day():
    start = datetime(2026, 6, 12, 7, 0, tzinfo=UTC)
    end = start + timedelta(days=1)
    csv_text = (
        "station,valid,tmpf,drct,sknt\n"
        "LAX,2026-06-12 20:00,80.0,270,10\n"
        "LAX,2026-06-13 07:00,90.0,270,10\n"  # exactly next local midnight — excluded
    )
    parsed = _parse_iem_asos_csv(csv_text, "LAX", start, end)
    assert parsed.record_count == 1
    assert parsed.peak_temp_f == 80.0


def test_local_climate_day_window_is_local_midnight(tmp_path):
    engine, _ = _make_engine(tmp_path)
    start_utc, end_utc = engine._local_climate_day_window()
    start_local = start_utc.astimezone(LAX_TZ)
    end_local = end_utc.astimezone(LAX_TZ)
    assert (start_local.hour, start_local.minute) == (0, 0)
    assert (end_local.hour, end_local.minute) == (0, 0)
    assert start_local.date() == datetime.now(LAX_TZ).date()
    assert end_local.date() == start_local.date() + timedelta(days=1)


# ── Observation staleness ────────────────────────────────────────────────────

class _FakeResp:
    def __init__(self, status=200, text=""):
        self.status = status
        self._text = text
        self.request_info = None
        self.history = ()

    async def text(self):
        return self._text

    async def json(self):
        raise ValueError("no json")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _FakeSession:
    """Returns queued responses in order; records requested URLs."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[str] = []

    def get(self, url, **kwargs):
        self.calls.append(str(url))
        return self._responses.pop(0)


def test_stale_iem_proxy_obs_triggers_fallback(tmp_path, monkeypatch):
    engine, _ = _make_engine(tmp_path)
    now = datetime.now(UTC)
    stale_ts = (now - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M")
    csv_text = f"station,valid,tmpf,drct,sknt\nBUR,{stale_ts},75.0,270,10\n"
    # IEM returns a 3-hour-old row; NWS fallback then fails → failed/stale obs
    engine.session = _FakeSession([_FakeResp(200, csv_text), _FakeResp(500)])
    monkeypatch.setattr(
        engine, "_local_climate_day_window",
        lambda: (now - timedelta(hours=12), now + timedelta(hours=12)),
    )

    proxy = pae.PROXY_STATIONS["LAX"][0]
    obs = asyncio.run(engine._fetch_proxy_obs(proxy))
    assert len(engine.session.calls) == 2  # IEM, then NWS fallback
    assert obs.source == "failed"
    assert obs.is_stale is True


def test_fresh_iem_proxy_obs_uses_real_timestamp(tmp_path, monkeypatch):
    engine, _ = _make_engine(tmp_path)
    now = datetime.now(UTC)
    fresh_dt = (now - timedelta(minutes=10)).replace(second=0, microsecond=0)
    fresh_ts = fresh_dt.strftime("%Y-%m-%d %H:%M")
    csv_text = f"station,valid,tmpf,drct,sknt\nBUR,{fresh_ts},75.0,270,10\n"
    engine.session = _FakeSession([_FakeResp(200, csv_text)])
    monkeypatch.setattr(
        engine, "_local_climate_day_window",
        lambda: (now - timedelta(hours=12), now + timedelta(hours=12)),
    )

    proxy = pae.PROXY_STATIONS["LAX"][0]
    obs = asyncio.run(engine._fetch_proxy_obs(proxy))
    assert obs.source == "iem_metar"
    assert obs.is_stale is False
    assert obs.observed_at == fresh_dt  # not "now"


def test_stale_target_current_temp_is_discounted(tmp_path, monkeypatch):
    engine, _ = _make_engine(tmp_path)
    peak = _fresh_peak(obs_age_min=180.0)  # 3-hour-old current reading
    _wire_signal_engine(engine, monkeypatch, peak)
    sig = asyncio.run(engine.evaluate_and_trade(brackets=[]))
    assert sig.current_target_temp_f is None
    assert any("stale" in r.lower() for r in sig.signal_reasons)


# ── dry_run defaults ─────────────────────────────────────────────────────────

def test_engine_dry_run_defaults_true(tmp_path):
    engine, _ = _make_engine(tmp_path)
    assert engine.dry_run is True


def test_run_proxy_scan_dry_run_defaults_true():
    assert inspect.signature(run_proxy_scan).parameters["dry_run"].default is True


# ── Scoring: today's brackets, fees, NO side ─────────────────────────────────

def _today_bracket(yes_bid=20, yes_ask=30, title="75-76°F", suffix="B76", **extra):
    mkt = {
        "ticker": _today_ticker(suffix),
        "title": title,
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
    }
    mkt.update(extra)
    return mkt


def test_scores_todays_brackets_not_tomorrows(tmp_path, monkeypatch):
    engine, _ = _make_engine(tmp_path)
    _wire_signal_engine(engine, monkeypatch, _fresh_peak())

    tomorrow_tag = (datetime.now(LAX_TZ) + timedelta(days=1)).strftime("%y%b%d").upper()
    brackets = [
        # Tomorrow's bracket priced absurdly cheap — would win if scored
        {"ticker": f"KXHIGHLAX-{tomorrow_tag}-B76", "title": "75-76°F",
         "yes_bid": 1, "yes_ask": 3},
        _today_bracket(),
    ]
    sig = asyncio.run(engine.evaluate_and_trade(brackets=brackets))
    assert sig.target_ticker == _today_ticker()
    assert sig.edge_passes is True


def test_dry_run_signal_places_no_order_and_skips_guards(tmp_path, monkeypatch):
    engine, client = _make_engine(tmp_path)  # dry_run default True
    guard_mock = _wire_signal_engine(engine, monkeypatch, _fresh_peak())
    sig = asyncio.run(engine.evaluate_and_trade(brackets=[_today_bracket()]))
    assert sig.edge_passes is True
    assert sig.trade_placed is False
    assert client.place_order.await_count == 0
    assert guard_mock.call_count == 0
    assert any("[DRY RUN]" in r for r in sig.signal_reasons)


def test_live_path_runs_trading_guards_and_blocks(tmp_path, monkeypatch):
    engine, client = _make_engine(tmp_path, dry_run=False)
    guard_mock = _wire_signal_engine(
        engine, monkeypatch, _fresh_peak(),
        guards_result=(False, ["FAIL: Kill switch active"]),
    )
    sig = asyncio.run(engine.evaluate_and_trade(brackets=[_today_bracket()]))

    assert guard_mock.call_count == 1
    kwargs = guard_mock.call_args.kwargs
    assert kwargs["city_key"] == "LAX"
    assert kwargs["dry_run"] is False  # live path must never bypass checks
    assert kwargs["trade_side"] == "yes"

    assert client.place_order.await_count == 0
    assert sig.trade_placed is False
    assert any("BLOCKED by pre-trade guards" in r for r in sig.signal_reasons)


def test_live_path_places_order_when_guards_pass(tmp_path, monkeypatch):
    engine, client = _make_engine(tmp_path, dry_run=False)
    guard_mock = _wire_signal_engine(engine, monkeypatch, _fresh_peak())
    sig = asyncio.run(engine.evaluate_and_trade(brackets=[_today_bracket()]))

    assert guard_mock.call_count == 1
    assert client.place_order.await_count == 1
    order_kwargs = client.place_order.await_args.kwargs
    assert order_kwargs["ticker"] == _today_ticker()
    assert order_kwargs["side"] == "yes"
    assert order_kwargs["price"] == 21  # bid+1
    assert sig.trade_placed is True
    assert sig.order_id == "ORD-1"


def test_ask_cross_charges_taker_fee(tmp_path, monkeypatch):
    engine, _ = _make_engine(tmp_path)
    _wire_signal_engine(engine, monkeypatch, _fresh_peak())
    # bid+1 == ask → marketable, fills as taker → fee deducted from edge
    sig_cross = asyncio.run(engine.evaluate_and_trade(
        brackets=[_today_bracket(yes_bid=20, yes_ask=21)]))

    second_dir = tmp_path / "b"
    second_dir.mkdir()
    engine2, _ = _make_engine(second_dir)
    _wire_signal_engine(engine2, monkeypatch, _fresh_peak())
    sig_maker = asyncio.run(engine2.evaluate_and_trade(
        brackets=[_today_bracket(yes_bid=20, yes_ask=30)]))

    fee = kalshi_taker_fee_cents(21)
    assert fee == 2
    assert sig_cross.edge == pytest.approx(sig_maker.edge - fee / 100.0, abs=1e-4)


def test_no_side_flagged_when_floor_excludes_bracket(tmp_path, monkeypatch):
    engine, _ = _make_engine(tmp_path)
    # Observed peak 75°F — the 70-71 bracket (window upper 72) is dead for YES
    _wire_signal_engine(engine, monkeypatch, _fresh_peak(peak_f=75.0))
    brackets = [_today_bracket(
        yes_bid=28, yes_ask=45, no_bid=55, no_ask=70,
        title="70-71°F", suffix="B71",
    )]
    sig = asyncio.run(engine.evaluate_and_trade(brackets=brackets))
    assert sig.trade_side == "no"
    assert sig.edge_passes is True
    assert sig.target_ticker == _today_ticker("B71")
    assert sig.model_prob > 0.9
    assert any("Would place NO" in r for r in sig.signal_reasons)


def test_no_side_not_flagged_when_bracket_still_live(tmp_path, monkeypatch):
    engine, _ = _make_engine(tmp_path)
    # Peak 71°F < window upper 72 → bracket can still settle YES; NO must not fire
    _wire_signal_engine(engine, monkeypatch, _fresh_peak(peak_f=71.0, current_f=71.0))
    brackets = [_today_bracket(
        yes_bid=0, yes_ask=45, no_bid=55, no_ask=70,
        title="70-71°F", suffix="B71",
    )]
    sig = asyncio.run(engine.evaluate_and_trade(brackets=brackets))
    assert sig.trade_side == "yes"  # default — NO branch never selected
    assert sig.edge_passes is False


def test_size_capped_by_top_of_book(tmp_path, monkeypatch):
    engine, client = _make_engine(tmp_path, dry_run=False)
    _wire_signal_engine(engine, monkeypatch, _fresh_peak(), tob_size=5)
    sig = asyncio.run(engine.evaluate_and_trade(brackets=[_today_bracket()]))
    assert sig.trade_placed is True
    assert client.place_order.await_args.kwargs["count"] == 5
    assert any("top-of-book" in r for r in sig.signal_reasons)


def test_empty_book_skips_trade(tmp_path, monkeypatch):
    engine, client = _make_engine(tmp_path, dry_run=False)
    _wire_signal_engine(engine, monkeypatch, _fresh_peak(), tob_size=0)
    sig = asyncio.run(engine.evaluate_and_trade(brackets=[_today_bracket()]))
    assert sig.edge_passes is False
    assert client.place_order.await_count == 0
