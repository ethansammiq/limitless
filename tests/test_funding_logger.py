"""funding_logger — ticker mapping, basis math, and print dedupe."""
from funding_logger import (
    build_snapshot_row,
    coinbase_product,
    dedupe_prints,
    perp_symbol,
)

# Real /margin/markets shape (2026-07-09): prices are dollar STRINGS per
# contract; KXBTCPERP contracts are 0.0001 BTC, so a 6.37 contract implies
# a 63,700 BTC.
BTC_MARKET = {
    "ticker": "KXBTCPERP",
    "status": "active",
    "bid": "6.3731",
    "ask": "6.3746",
    "contract_size": "0.000100",
    "reference_price": {"price": "6.3722", "ts_ms": 1783648892000},
    "open_interest_notional_value_dollars": "4958858.125200",
    "volume_24h_notional_value_dollars": "832515.562900",
}

BTC_ESTIMATE = {
    "market_ticker": "KXBTCPERP",
    "funding_rate": 0,
    "mark_price": "6.3760",
    "next_funding_time": "2026-07-10T04:00:00Z",
}


def test_perp_symbol_strips_kx_and_perp():
    assert perp_symbol("KXBTCPERP") == "BTC"
    assert perp_symbol("KXHYPEPERP") == "HYPE"


def test_kshib_maps_to_plain_shib_spot():
    assert perp_symbol("KXKSHIBPERP") == "SHIB"
    assert coinbase_product("KXKSHIBPERP") == "SHIB-USD"


def test_kshib_basis_scales_kshib_units_to_shib():
    # Kalshi denominates in kSHIB: contract_size 1000 = 1000 kSHIB = 1M SHIB.
    # Live 2026-07-09: mid 4.3391, SHIB-USD 4.345e-06 -> ~-13.6 bps, not +9.9M.
    market = {
        "ticker": "KXKSHIBPERP",
        "bid": "4.3371",
        "ask": "4.3411",
        "contract_size": "1000.000000",
    }
    row = build_snapshot_row(market, None, cb_spot=4.345e-06,
                             ts="2026-07-10T02:00:00+00:00")
    assert row["implied_spot"] == round(4.3391 / 1_000_000, 8)
    assert -20 < row["basis_bps"] < 0


def test_snapshot_row_basis_math():
    row = build_snapshot_row(BTC_MARKET, BTC_ESTIMATE, cb_spot=63714.235,
                             ts="2026-07-10T02:00:00+00:00")
    assert row["mid"] == (6.3731 + 6.3746) / 2
    # mid / contract_size: 6.37385 / 0.0001 = 63,738.5 implied BTC
    assert row["implied_spot"] == 63738.5
    # (63738.5 - 63714.235) / 63714.235 * 1e4 = +3.81 bps (perp rich)
    assert row["basis_bps"] == 3.81
    assert row["funding_est"] == 0.0
    assert row["next_funding_time"] == "2026-07-10T04:00:00Z"
    assert row["sym"] == "BTC"


def test_snapshot_row_survives_missing_spot_and_estimate():
    # HYPE has no Coinbase listing; estimate fetch can fail — row still logs
    row = build_snapshot_row(BTC_MARKET, None, cb_spot=None,
                             ts="2026-07-10T02:00:00+00:00")
    assert row["basis_bps"] is None
    assert row["cb_spot"] is None
    assert row["funding_est"] is None
    assert row["mid"] is not None  # perp side always captured


def test_dedupe_prints_keeps_only_fresh_oldest_first():
    fetched = [
        {"market_ticker": "KXBTCPERP", "funding_time": "2026-07-09T20:00:00Z", "funding_rate": 0},
        {"market_ticker": "KXHYPEPERP", "funding_time": "2026-07-09T12:00:00Z", "funding_rate": -0.00011},
        {"market_ticker": "KXBTCPERP", "funding_time": "2026-07-09T12:00:00Z", "funding_rate": 0},
    ]
    existing = {("KXBTCPERP", "2026-07-09T12:00:00Z")}
    fresh = dedupe_prints(fetched, existing)
    assert [(p["market_ticker"], p["funding_time"]) for p in fresh] == [
        ("KXHYPEPERP", "2026-07-09T12:00:00Z"),
        ("KXBTCPERP", "2026-07-09T20:00:00Z"),
    ]
