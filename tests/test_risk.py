"""Tests for core/risk.py — the single source of money math and risk caps."""
from core import risk


class TestCostModel:
    def test_buy_yes_costs_price(self):
        assert risk.order_cost_dollars("buy", "yes", 40, 16) == 6.40

    def test_sell_collateralizes_complement(self):
        # selling YES at 22c: worst case is the 78c complement per contract
        assert risk.order_cost_dollars("sell", "yes", 20, 22) == 15.60


class TestClampCount:
    def test_buy_clamps_to_notional_cap(self):
        # the 2026-07-12 DAL alert: 60134 × 1¢ = $601 book depth, $50 cap
        assert risk.clamp_count("buy", "yes", 60134, 1, 50.0) == 5000

    def test_sell_clamps_on_complement_collateral(self):
        # selling YES at 22¢ collateralizes 78¢/contract → 64 fit in $50
        assert risk.clamp_count("sell", "yes", 100, 22, 50.0) == 64

    def test_small_orders_pass_through(self):
        assert risk.clamp_count("buy", "yes", 23, 18, 50.0) == 23

    def test_unaffordable_single_contract_is_zero(self):
        assert risk.clamp_count("buy", "yes", 10, 99, 0.5) == 0


class TestStationNightKey:
    """One cap bucket per station-night — high and low ladders included.
    Series names are irregular, so the registry does the mapping."""

    def test_high_and_low_ladders_share_the_station_night(self):
        assert (risk.station_night_key("KXHIGHNY-26JUL14-T90")
                == risk.station_night_key("KXLOWTNYC-26JUL14-B70.5")
                == "NYC:26JUL14")

    def test_asymmetric_series_pairs_resolve_via_the_registry(self):
        for high, low, awips in (("KXHIGHCHI", "KXLOWTCHI", "MDW"),
                                 ("KXHIGHTDAL", "KXLOWTDAL", "DFW"),
                                 ("KXHIGHTMIN", "KXLOWTMIN", "MSP")):
            assert (risk.station_night_key(f"{high}-26JUL14-T90")
                    == risk.station_night_key(f"{low}-26JUL14-B70.5")
                    == f"{awips}:26JUL14")

    def test_different_nights_stay_separate(self):
        assert (risk.station_night_key("KXHIGHNY-26JUL14-T90")
                != risk.station_night_key("KXHIGHNY-26JUL15-T90"))

    def test_unknown_series_falls_back_to_the_v1_key(self):
        assert risk.station_night_key("KXFOO-26JUL14-T90") == "KXFOO-26JUL14"

    def test_malformed_ticker_never_raises(self):
        assert risk.station_night_key("T1") == "T1"
        assert risk.station_night_key("") == ""


class TestOneSourceOfTruth:
    """The constants exist exactly once; consumers alias, never redefine."""

    def test_entry_cap_is_the_standing_20c_rule(self):
        assert risk.MAX_ENTRY_ASK_C == 20

    def test_take_queue_and_metar_sniper_share_the_entry_cap(self):
        import metar_sniper
        from core import take_queue

        assert take_queue.MAX_STAGE_ASK_C is risk.MAX_ENTRY_ASK_C
        assert metar_sniper.MAX_BUY_ASK_C is risk.MAX_ENTRY_ASK_C

    def test_snipers_share_the_wall_ask_signature(self):
        import cli_sniper
        import metar_sniper
        from core import walls

        assert walls.WALL_ASK_DEPTH == 10_000
        assert cli_sniper.WALL_ASK_DEPTH is walls.WALL_ASK_DEPTH
        assert metar_sniper.WALL_ASK_DEPTH is walls.WALL_ASK_DEPTH

    def test_take_and_scorecard_source_the_fixed_notional_cap(self):
        from backtest import sniper_scorecard
        from scripts import take

        assert risk.DEFAULT_MAX_NOTIONAL == 50.0
        assert take.DEFAULT_MAX_NOTIONAL is risk.DEFAULT_MAX_NOTIONAL
        # scorecard stays env-or-fixed (reproducible grading) but the fixed
        # number comes from here
        assert sniper_scorecard.max_notional_dollars() == risk.DEFAULT_MAX_NOTIONAL
