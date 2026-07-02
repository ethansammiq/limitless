"""Tests for dead_bracket_sweeper pure helpers (no network)."""
import dead_bracket_sweeper as dbs


class TestParseSubtitle:
    def test_below(self):
        assert dbs.parse_subtitle("98° or below") == (None, 98.0)

    def test_range(self):
        assert dbs.parse_subtitle("99° to 100°") == (99.0, 100.0)

    def test_above(self):
        assert dbs.parse_subtitle("107° or above") == (107.0, None)

    def test_negative_lows(self):
        assert dbs.parse_subtitle("-5° or below") == (None, -5.0)
        assert dbs.parse_subtitle("-4° to -3°") == (-4.0, -3.0)

    def test_garbage_is_none(self):
        assert dbs.parse_subtitle("") is None
        assert dbs.parse_subtitle(None) is None
        assert dbs.parse_subtitle("above 98") is None


class TestCertainSettleBounds:
    def test_exact_ob_holds(self):
        # 2026-07-02 live case: 100.0°F ob -> CLI settles >= 100
        assert dbs.certain_min_settle(100.0) == 100

    def test_half_degree_backs_off(self):
        # 99.5°F reported could be a true 99.41 -> only >= 99 is certain
        assert dbs.certain_min_settle(99.5) == 99

    def test_low_mirror(self):
        assert dbs.certain_max_settle(63.0) == 63
        assert dbs.certain_max_settle(62.5) == 63

    def test_negative_low(self):
        assert dbs.certain_max_settle(-5.0) == -5


class TestIsDead:
    def test_high_tail_dead(self):
        # "98 or below" with a certain 100 settle
        assert dbs.is_dead("high", None, 98.0, 100)

    def test_high_bracket_at_bound_alive(self):
        # "99 to 100" can still win when CLI settles exactly 100
        assert not dbs.is_dead("high", 99.0, 100.0, 100)

    def test_high_open_top_never_dead(self):
        assert not dbs.is_dead("high", 107.0, None, 100)

    def test_low_bracket_dead(self):
        # "64 or above" when the running min already hit 63
        assert dbs.is_dead("low", 64.0, None, 63)

    def test_low_bracket_at_bound_alive(self):
        assert not dbs.is_dead("low", 63.0, 64.0, 63)


class TestCorroboratedExtreme:
    def test_corroborated_max(self):
        assert dbs.corroborated_extreme([84.9, 96.1, 100.0, 99.0], "high") == 100.0

    def test_lone_spike_rejected(self):
        assert dbs.corroborated_extreme([84.9, 85.1, 100.0], "high") is None

    def test_hourly_warmup_gap_accepted(self):
        # KDEN 2026-07-02: 81.0 -> 84.9 between hourly obs is real warming
        assert dbs.corroborated_extreme([75.9, 81.0, 84.9], "high") == 84.9

    def test_min_side(self):
        assert dbs.corroborated_extreme([70.0, 63.2, 63.0], "low") == 63.0

    def test_single_ob_rejected(self):
        assert dbs.corroborated_extreme([100.0], "high") is None
        assert dbs.corroborated_extreme([], "high") is None


class TestBidProceeds:
    def test_live_case_levels(self):
        # T99 book at find time: fee = ceil(0.07*p*(100-p)/100) per contract
        bids = [[5, 5], [22, 85], [26, 297], [38, 5], [42, 45]]
        net, contracts, levels = dbs.bid_proceeds_cents(bids)
        assert contracts == 437
        assert levels == [[42, 45], [38, 5], [26, 297], [22, 85], [5, 5]]
        assert net == (5 - 1) * 5 + (22 - 2) * 85 + (26 - 2) * 297 + (38 - 2) * 5 + (42 - 2) * 45

    def test_dust_filtered(self):
        net, contracts, _ = dbs.bid_proceeds_cents([[4, 500], [1, 999]])
        assert net == 0
        assert contracts == 0

    def test_empty_book(self):
        assert dbs.bid_proceeds_cents([]) == (0, 0, [])
        assert dbs.bid_proceeds_cents(None) == (0, 0, [])


class TestAlertDedup:
    def test_new_ticker_alerts(self):
        assert dbs.should_alert({}, "KXHIGHNY-26JUL02-T99", 1000)

    def test_known_ticker_suppressed(self):
        state = {"KXHIGHNY-26JUL02-T99": {"net_cents": 1000, "ts": "2026-07-02T20:00:00+00:00"}}
        assert not dbs.should_alert(state, "KXHIGHNY-26JUL02-T99", 1100)

    def test_grown_net_realerts(self):
        state = {"KXHIGHNY-26JUL02-T99": {"net_cents": 1000, "ts": "2026-07-02T20:00:00+00:00"}}
        assert dbs.should_alert(state, "KXHIGHNY-26JUL02-T99", 1250)


class TestFormatAlert:
    def test_title_and_body(self):
        findings = [{
            "ticker": "KXHIGHNY-26JUL02-T99", "subtitle": "98° or below",
            "kind": "high", "city": "NYC", "station": "KNYC",
            "extreme_f": 100.0, "certain_settle": 100,
            "net_cents": 10828, "contracts": 437,
            "levels": [[42, 45], [26, 297]],
        }]
        title, body = dbs.format_alert(findings)
        assert "$108.28" in title
        assert "KNYC runmax 100.0°F" in body
        assert "settles ≥100°" in body
        assert "42¢×45" in body
