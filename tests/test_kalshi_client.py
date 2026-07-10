#!/usr/bin/env python3
"""
Tests for Kalshi API client.
"""

import asyncio
import uuid

import pytest

from kalshi_client import KalshiClient, KalshiAPIError, KalshiRateLimitError, normalize_order


# =============================================================================
# FIXTURES
# =============================================================================

@pytest.fixture
def client():
    """Create a KalshiClient instance for testing."""
    return KalshiClient(
        api_key_id="test-api-key",
        private_key_path="",
        demo_mode=True,
    )


# =============================================================================
# INITIALIZATION TESTS
# =============================================================================

class TestInitialization:
    """Tests for client initialization."""

    def test_demo_mode_url(self):
        """Demo mode should use demo URL."""
        client = KalshiClient(demo_mode=True)
        assert "demo" in client.base_url.lower()

    def test_live_mode_url(self):
        """Live mode should use production URL."""
        client = KalshiClient(demo_mode=False)
        assert "elections" in client.base_url.lower()

    def test_initial_counters(self, client):
        """Request counters should start at zero."""
        assert client._request_count == 0
        assert client._error_count == 0


# =============================================================================
# ERROR CLASS TESTS
# =============================================================================

class TestErrors:
    """Tests for error classes."""

    def test_api_error(self):
        """KalshiAPIError should contain status and message."""
        error = KalshiAPIError(400, "Bad request")
        assert error.status == 400
        assert "400" in str(error)
        assert "Bad request" in str(error)

    def test_rate_limit_error(self):
        """KalshiRateLimitError should contain retry_after."""
        error = KalshiRateLimitError(retry_after=30)
        assert error.status == 429
        assert error.retry_after == 30
        assert "30" in str(error)


# =============================================================================
# MONOTONIC TIMESTAMP TESTS
# =============================================================================

class TestMonotonicTimestamp:
    """Tests for the monotonic timestamp counter that prevents duplicate signatures."""

    def test_monotonic_always_increases(self):
        """Consecutive calls must return strictly increasing values."""
        ts1 = KalshiClient._monotonic_ts_ms()
        ts2 = KalshiClient._monotonic_ts_ms()
        ts3 = KalshiClient._monotonic_ts_ms()
        assert ts2 > ts1
        assert ts3 > ts2

    def test_monotonic_no_duplicates_rapid_fire(self):
        """100 rapid-fire calls must all be unique."""
        timestamps = [KalshiClient._monotonic_ts_ms() for _ in range(100)]
        assert len(set(timestamps)) == 100

    def test_monotonic_is_reasonable_ms(self):
        """Timestamp should be in plausible millisecond range."""
        import time
        ts = KalshiClient._monotonic_ts_ms()
        now_ms = int(time.time() * 1000)
        # Should be within 1 second of real time
        assert abs(ts - now_ms) < 1000


# =============================================================================
# V2 ORDER ENDPOINT TESTS
# =============================================================================

def _capture_requests(client, response):
    """Replace client._req_safe with a stub that records calls."""
    calls = []

    async def fake_req_safe(method, path, data=None, auth=False):
        calls.append({"method": method, "path": path, "data": data, "auth": auth})
        return response

    client._req_safe = fake_req_safe
    return calls


class TestPlaceOrderV2:
    """place_order translates the V1 signature to the V2 single-book schema."""

    def test_buy_yes_translates_to_bid(self, client):
        calls = _capture_requests(client, {"order_id": "o1", "remaining_count": "10.00"})
        asyncio.run(client.place_order("KXHIGHNY-T1", "yes", "buy", 10, 12))
        call = calls[0]
        assert call["method"] == "POST"
        assert call["path"] == "/portfolio/events/orders"
        assert call["auth"] is True
        assert call["data"]["side"] == "bid"
        assert call["data"]["price"] == "0.1200"
        assert call["data"]["count"] == "10.00"
        assert call["data"]["time_in_force"] == "good_till_canceled"
        assert call["data"]["self_trade_prevention_type"] == "taker_at_cross"
        # V1-only fields must not leak into the V2 payload
        assert "action" not in call["data"]
        assert "yes_price" not in call["data"]
        assert "type" not in call["data"]

    def test_sell_yes_translates_to_ask(self, client):
        calls = _capture_requests(client, {"order_id": "o1"})
        asyncio.run(client.place_order("KXHIGHNY-T1", "yes", "sell", 5, 90))
        assert calls[0]["data"]["side"] == "ask"
        assert calls[0]["data"]["price"] == "0.9000"

    def test_buy_no_translates_to_ask_at_complement(self, client):
        calls = _capture_requests(client, {"order_id": "o1"})
        asyncio.run(client.place_order("KXHIGHNY-T1", "no", "buy", 5, 88))
        assert calls[0]["data"]["side"] == "ask"
        assert calls[0]["data"]["price"] == "0.1200"

    def test_sell_no_translates_to_bid_at_complement(self, client):
        calls = _capture_requests(client, {"order_id": "o1"})
        asyncio.run(client.place_order("KXHIGHNY-T1", "no", "sell", 5, 88))
        assert calls[0]["data"]["side"] == "bid"
        assert calls[0]["data"]["price"] == "0.1200"

    def test_invalid_side_raises_before_any_request(self, client):
        calls = _capture_requests(client, {"order_id": "o1"})
        with pytest.raises(ValueError):
            asyncio.run(client.place_order("KXHIGHNY-T1", "bid", "buy", 5, 12))
        assert calls == []

    def test_client_order_id_passes_through_verbatim(self, client):
        calls = _capture_requests(client, {"order_id": "o1"})
        cid = "11111111-2222-3333-4444-555555555555"
        result = asyncio.run(
            client.place_order("KXHIGHNY-T1", "yes", "buy", 5, 12, client_order_id=cid)
        )
        assert calls[0]["data"]["client_order_id"] == cid
        assert result["order"]["client_order_id"] == cid

    def test_client_order_id_generated_when_absent(self, client):
        calls = _capture_requests(client, {"order_id": "o1"})
        asyncio.run(client.place_order("KXHIGHNY-T1", "yes", "buy", 5, 12))
        # Must be a valid UUID for Kalshi's 24h dedup window
        uuid.UUID(calls[0]["data"]["client_order_id"])

    def test_expiration_time_included_when_set(self, client):
        calls = _capture_requests(client, {"order_id": "o1"})
        asyncio.run(
            client.place_order("KXHIGHNY-T1", "yes", "buy", 5, 12, expiration_time=1783200900)
        )
        assert calls[0]["data"]["expiration_time"] == 1783200900

    def test_expiration_time_omitted_by_default(self, client):
        calls = _capture_requests(client, {"order_id": "o1"})
        asyncio.run(client.place_order("KXHIGHNY-T1", "yes", "buy", 5, 12))
        assert "expiration_time" not in calls[0]["data"]

    def test_market_order_maps_to_immediate_or_cancel(self, client):
        calls = _capture_requests(client, {"order_id": "o1"})
        asyncio.run(client.place_order("KXHIGHNY-T1", "yes", "buy", 5, 12, order_type="market"))
        assert calls[0]["data"]["time_in_force"] == "immediate_or_cancel"

    def test_result_wrapped_with_synthesized_resting_status(self, client):
        _capture_requests(client, {"order_id": "o1", "fill_count": "0.00", "remaining_count": "5.00"})
        result = asyncio.run(client.place_order("KXHIGHNY-T1", "yes", "buy", 5, 12))
        order = result["order"]
        assert order["order_id"] == "o1"
        assert order["status"] == "resting"
        assert order["side"] == "yes"
        assert order["action"] == "buy"

    def test_result_executed_when_fully_filled(self, client):
        _capture_requests(client, {"order_id": "o1", "fill_count": "5.00", "remaining_count": "0.00"})
        result = asyncio.run(client.place_order("KXHIGHNY-T1", "yes", "buy", 5, 12))
        assert result["order"]["status"] == "executed"

    def test_explicit_status_preserved(self, client):
        _capture_requests(client, {"order": {"order_id": "o1", "status": "canceled"}})
        result = asyncio.run(client.place_order("KXHIGHNY-T1", "yes", "buy", 5, 12))
        assert result["order"]["status"] == "canceled"

    def test_empty_response_returns_empty_dict(self, client):
        _capture_requests(client, {})
        result = asyncio.run(client.place_order("KXHIGHNY-T1", "yes", "buy", 5, 12))
        assert result == {}


class TestGetOrdersV2:
    """get_orders stays on GET /portfolio/orders and normalizes V2 objects."""

    def test_path_and_params(self, client):
        calls = _capture_requests(client, {"orders": []})
        asyncio.run(client.get_orders(ticker="KXHIGHNY-T1", status="resting"))
        assert calls[0]["method"] == "GET"
        assert calls[0]["path"] == "/portfolio/orders?status=resting&ticker=KXHIGHNY-T1"
        assert calls[0]["auth"] is True

    def test_orders_are_normalized(self, client):
        _capture_requests(client, {"orders": [{
            "order_id": "o1",
            "ticker": "KXHIGHNY-T1",
            "outcome_side": "yes",
            "book_side": "bid",
            "yes_price_dollars": "0.1200",
            "remaining_count_fp": "5.00",
        }]})
        orders = asyncio.run(client.get_orders())
        assert orders[0]["action"] == "buy"
        assert orders[0]["side"] == "yes"
        assert orders[0]["yes_price"] == 12
        assert orders[0]["remaining_count"] == 5


class TestCancelOrderV2:
    """cancel_order uses the V2 events path."""

    def test_delete_v2_path(self, client):
        calls = _capture_requests(client, {"order_id": "o1", "reduced_by": "5.00"})
        result = asyncio.run(client.cancel_order("o1"))
        assert calls[0]["method"] == "DELETE"
        assert calls[0]["path"] == "/portfolio/events/orders/o1"
        assert calls[0]["auth"] is True
        assert result["order_id"] == "o1"


class TestNormalizeOrder:
    """normalize_order backfills legacy V1 fields from the V2 order schema."""

    @pytest.mark.parametrize("outcome, book, action", [
        ("yes", "bid", "buy"),
        ("yes", "ask", "sell"),
        ("no", "ask", "buy"),
        ("no", "bid", "sell"),
    ])
    def test_action_backfill(self, outcome, book, action):
        order = normalize_order({"outcome_side": outcome, "book_side": book})
        assert order["action"] == action
        assert order["side"] == outcome

    def test_prices_and_counts_backfilled(self):
        order = normalize_order({
            "outcome_side": "no",
            "book_side": "ask",
            "no_price_dollars": "0.8800",
            "fill_count_fp": "3.00",
            "remaining_count_fp": "2.00",
            "initial_count_fp": "5.00",
        })
        assert order["no_price"] == 88
        assert order["fill_count"] == 3
        assert order["remaining_count"] == 2
        assert order["initial_count"] == 5

    def test_legacy_paper_order_untouched(self):
        paper = {"order_id": "paper_1", "side": "yes", "action": "buy",
                 "count": 5, "price": 12, "status": "RESTING"}
        assert normalize_order(dict(paper)) == paper

    def test_existing_legacy_fields_not_overwritten(self):
        order = normalize_order({"side": "no", "action": "sell",
                                 "outcome_side": "yes", "book_side": "bid"})
        assert order["side"] == "no"
        assert order["action"] == "sell"


# =============================================================================
# RUN TESTS
# =============================================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v"])


class TestCheckedReads:
    """Degraded reads must be distinguishable from real empty results
    (2026-07-06 review: false $0.00 balance + sniper silent-loss)."""

    def test_get_balance_checked_real(self, client):
        _capture_requests(client, {"balance": 11759})
        assert asyncio.run(client.get_balance_checked()) == 117.59

    def test_get_balance_checked_degraded_is_none(self, client):
        _capture_requests(client, {})          # _req_safe swallowed an error
        assert asyncio.run(client.get_balance_checked()) is None

    def test_get_balance_still_collapses_to_zero(self, client):
        # legacy float contract preserved for the 12 other callers
        _capture_requests(client, {})
        assert asyncio.run(client.get_balance()) == 0.0

    def test_get_markets_checked_real_empty(self, client):
        _capture_requests(client, {"markets": []})
        markets, ok = asyncio.run(client.get_markets_checked(series_ticker="KXHIGHCHI"))
        assert markets == [] and ok is True     # series genuinely has no markets

    def test_get_markets_checked_degraded(self, client):
        _capture_requests(client, {})
        markets, ok = asyncio.run(client.get_markets_checked(series_ticker="KXHIGHCHI"))
        assert markets == [] and ok is False    # request failed — caller must not trust []


class TestAuthFailureIsLoud:
    """A silent {} from a 401 read as 'no positions / $0 balance' for an
    entire evening (2026-07-09). Auth errors must raise, not degrade."""

    def test_auth_error_not_an_api_error(self):
        from kalshi_client import KalshiAuthError
        assert not issubclass(KalshiAuthError, KalshiAPIError)  # -> no retry

    def test_req_safe_reraises_auth(self):
        import asyncio
        from kalshi_client import KalshiAuthError
        client = KalshiClient(demo_mode=True)

        async def raise_auth(*a, **k):
            raise KalshiAuthError(401, "token authentication failure")
        client._req = raise_auth

        async def go():
            return await client._req_safe("GET", "/portfolio/balance", auth=True)
        try:
            asyncio.run(go())
            raise AssertionError("KalshiAuthError was swallowed")
        except KalshiAuthError:
            pass

    def test_req_safe_still_degrades_transient(self):
        import asyncio
        client = KalshiClient(demo_mode=True)

        async def raise_api(*a, **k):
            raise KalshiAPIError(500, "internal")
        client._req = raise_api

        async def go():
            return await client._req_safe("GET", "/markets")
        assert asyncio.run(go()) == {}


class TestConstructorDefaults:
    """Bare KalshiClient() used to mean demo-api silently — an evening of
    scans (2026-07-09) priced live trades off demo furniture."""

    def test_default_is_live_not_demo(self, monkeypatch):
        monkeypatch.delenv("KALSHI_DEMO_MODE", raising=False)
        client = KalshiClient()
        assert "demo" not in client.base_url.lower()

    def test_env_can_opt_into_demo(self, monkeypatch):
        monkeypatch.setenv("KALSHI_DEMO_MODE", "true")
        client = KalshiClient()
        assert "demo" in client.base_url.lower()

    def test_credentials_default_from_env(self, monkeypatch):
        monkeypatch.setenv("KALSHI_API_KEY_ID", "env-key")
        monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", "/env/key.pem")
        client = KalshiClient(demo_mode=False)
        assert client.api_key_id == "env-key"
        assert client.private_key_path == "/env/key.pem"

    def test_explicit_args_beat_env(self, monkeypatch):
        monkeypatch.setenv("KALSHI_API_KEY_ID", "env-key")
        client = KalshiClient(api_key_id="explicit", demo_mode=False)
        assert client.api_key_id == "explicit"
