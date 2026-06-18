#!/usr/bin/env python3
"""
Tests for Kalshi API client.
"""

import pytest

from kalshi_client import KalshiClient, KalshiAPIError, KalshiRateLimitError


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
# RUN TESTS
# =============================================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
