"""Tests for alert-decay pure helpers (no network)."""
from backtest.alert_decay import decay_rows, price_at_offsets, summarize


def _candle(end_ts, ask_c=None, bid_c=None):
    c = {"end_period_ts": end_ts}
    if ask_c is not None:
        c["yes_ask"] = {"close_dollars": f"{ask_c / 100:.4f}"}
    if bid_c is not None:
        c["yes_bid"] = {"close_dollars": f"{bid_c / 100:.4f}"}
    return c


ALERT = 1_000_000


class TestPriceAtOffsets:
    def test_last_known_at_or_before_cutoff(self):
        candles = [_candle(ALERT + 60, ask_c=16), _candle(ALERT + 120, ask_c=40),
                   _candle(ALERT + 300, ask_c=90)]
        at = price_at_offsets(candles, ALERT, "yes_ask")
        assert at[1] == 16.0
        assert at[2] == 40.0
        assert at[5] == 90.0
        assert at[10] == 90.0   # no later candle — last known carries
        assert at[20] == 90.0

    def test_no_candles_is_none(self):
        at = price_at_offsets([], ALERT, "yes_ask")
        assert all(v is None for v in at.values())

    def test_sell_dead_tracks_bid(self):
        candles = [_candle(ALERT + 60, bid_c=9), _candle(ALERT + 600, bid_c=2)]
        at = price_at_offsets(candles, ALERT, "yes_bid")
        assert at[1] == 9.0 and at[10] == 2.0


class TestDecayRows:
    def test_buy_winner_row(self):
        findings = [{"ticker": "T1", "kind": "buy_winner", "ask": 16,
                     "is_final": False, "ts": "1970-01-12T13:46:40+00:00"}]
        rows = decay_rows(findings, {"T1": [_candle(ALERT + 120, ask_c=40)]})
        assert rows[0]["detected_cents"] == 16
        assert rows[0]["at_offsets"][2] == 40.0

    def test_summarize_reports_median_delta(self):
        findings = [{"ticker": "T1", "kind": "buy_winner", "ask": 16,
                     "is_final": True, "ts": "1970-01-12T13:46:40+00:00"}]
        rows = decay_rows(findings, {"T1": [_candle(ALERT + 120, ask_c=40)]})
        out = summarize(rows)
        assert "+2m: +24¢ (n=1)" in out
        assert "final buys (1)" in out


class TestMetarJournalLoader:
    def test_flatten_and_suppressed_excluded(self, tmp_path):
        import json
        from backtest.alert_decay import load_metar_findings
        row = {"ts": "2026-07-12T05:50:00+00:00", "station": "KMSP",
               "kind": "max", "findings": [
                   {"ticker": "KXHIGHTMIN-26JUL12-B90.5", "kind": "buy_winner",
                    "ask": 14, "ladder_kind": "high"},
                   {"ticker": "KXLOWTMIN-26JUL12-B68.5", "kind": "buy_winner",
                    "ask": 9, "ladder_kind": "low",
                    "suppressed": "low_ceiling_forecast"}]}
        (tmp_path / "2026-07-12.jsonl").write_text(json.dumps(row) + "\n")
        out = load_metar_findings(journal_dir=tmp_path)
        assert len(out) == 1
        f = out[0]
        assert f["ticker"] == "KXHIGHTMIN-26JUL12-B90.5"
        assert f["ts"] == row["ts"] and f["final"] is False and f["ask"] == 14
