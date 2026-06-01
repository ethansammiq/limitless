#!/usr/bin/env python3
"""Kalshi API Client - RSA-PSS authenticated trading for prediction markets."""

import asyncio
import base64
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

import aiohttp
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    before_sleep_log,
)

from config import (
    KALSHI_LIVE_URL,
    KALSHI_DEMO_URL,
    API_MIN_REQUEST_INTERVAL,
    API_RETRY_ATTEMPTS,
    API_RETRY_MIN_WAIT_SEC,
    API_RETRY_MAX_WAIT_SEC,
    API_RETRY_MULTIPLIER,
    HTTP_TIMEOUT_TOTAL_SEC,
    HTTP_TIMEOUT_CONNECT_SEC,
    CONNECTION_POOL_LIMIT,
    DNS_CACHE_TTL_SEC,
    KEEPALIVE_TIMEOUT_SEC,
    ORDERBOOK_DEPTH,
)

logger = logging.getLogger(__name__)

__all__ = ["KalshiClient", "KalshiAPIError", "KalshiRateLimitError", "fetch_balance_quick"]


class KalshiAPIError(Exception):
    """Raised when Kalshi API returns an error."""
    def __init__(self, status: int, message: str = ""):
        self.status = status
        self.message = message
        super().__init__(f"Kalshi API error {status}: {message}")


class KalshiRateLimitError(KalshiAPIError):
    """Raised when rate limited by Kalshi API."""
    def __init__(self, retry_after: int = 0):
        self.retry_after = retry_after
        super().__init__(429, f"Rate limited. Retry after {retry_after}s")


class KalshiClient:
    """Async client for Kalshi trading API with retry logic."""

    # Monotonic timestamp counter to prevent duplicate signatures.
    # Two requests within the same millisecond would produce identical
    # signatures without this — the counter ensures strict ordering.
    _ts_lock = threading.Lock()
    _last_ts = 0

    @classmethod
    def _monotonic_ts_ms(cls) -> int:
        """Return a strictly increasing millisecond timestamp."""
        with cls._ts_lock:
            ts = int(time.time() * 1000)
            if ts <= cls._last_ts:
                ts = cls._last_ts + 1
            cls._last_ts = ts
            return ts

    def __init__(self, api_key_id: str = "", private_key_path: str = "", demo_mode: bool = True):
        self.api_key_id = api_key_id
        self.private_key_path = private_key_path
        self.demo_mode = demo_mode
        self.base_url = KALSHI_DEMO_URL if demo_mode else KALSHI_LIVE_URL
        self.private_key = None
        self.session: Optional[aiohttp.ClientSession] = None
        self.last_request_time = 0.0
        self._request_count = 0
        self._error_count = 0

    async def start(self):
        """Initialize the client session and load credentials."""
        self.session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(
                limit=CONNECTION_POOL_LIMIT,
                ttl_dns_cache=DNS_CACHE_TTL_SEC,
                keepalive_timeout=KEEPALIVE_TIMEOUT_SEC,
            ),
            timeout=aiohttp.ClientTimeout(
                total=HTTP_TIMEOUT_TOTAL_SEC,
                connect=HTTP_TIMEOUT_CONNECT_SEC,
            ),
        )
        if self.private_key_path and Path(self.private_key_path).exists():
            pw_env = __import__("os").getenv("KALSHI_KEY_PASSWORD")
            pw = pw_env.encode() if pw_env else None
            try:
                self.private_key = serialization.load_pem_private_key(
                    Path(self.private_key_path).read_bytes(), password=pw
                )
            except TypeError:
                raise RuntimeError(
                    "Private key appears encrypted. Set KALSHI_KEY_PASSWORD env var."
                )
            except (ValueError, Exception) as exc:
                raise RuntimeError(f"Failed to load private key at {self.private_key_path}: {exc}") from exc
            logger.info("Kalshi client initialized with credentials")
        else:
            logger.warning("Kalshi client initialized WITHOUT credentials (public endpoints only)")

    async def stop(self):
        """Close the client session."""
        if self.session:
            await self.session.close()
            logger.info(f"Kalshi client stopped. Requests: {self._request_count}, Errors: {self._error_count}")

    def _sign(self, method: str, path: str) -> dict:
        """Generate RSA-PSS signature for authenticated requests."""
        if self.private_key is None:
            raise RuntimeError("Private key not loaded — cannot sign request. Check KALSHI_PRIVATE_KEY_PATH.")
        ts = str(self._monotonic_ts_ms())
        msg = f"{ts}{method}/trade-api/v2{path.split('?')[0]}"
        sig = base64.b64encode(
            self.private_key.sign(
                msg.encode(),
                padding.PSS(
                    mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.DIGEST_LENGTH,
                ),
                hashes.SHA256(),
            )
        ).decode()
        return {
            "Content-Type": "application/json",
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-SIGNATURE": sig,
            "KALSHI-ACCESS-TIMESTAMP": ts,
        }

    async def _rate_limit(self):
        """Enforce minimum interval between requests."""
        elapsed = time.time() - self.last_request_time
        if elapsed < API_MIN_REQUEST_INTERVAL:
            await asyncio.sleep(API_MIN_REQUEST_INTERVAL - elapsed)
        self.last_request_time = time.time()

    @retry(
        stop=stop_after_attempt(API_RETRY_ATTEMPTS),
        wait=wait_exponential(
            multiplier=API_RETRY_MULTIPLIER,
            min=API_RETRY_MIN_WAIT_SEC,
            max=API_RETRY_MAX_WAIT_SEC,
        ),
        retry=retry_if_exception_type((aiohttp.ClientError, asyncio.TimeoutError, KalshiRateLimitError, KalshiAPIError)),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
    async def _req(self, method: str, path: str, data: dict = None, auth: bool = False) -> dict:
        """Make an API request with automatic retry on transient failures."""
        await self._rate_limit()
        self._request_count += 1

        headers = self._sign(method, path) if auth and self.private_key else {"Content-Type": "application/json"}

        try:
            async with getattr(self.session, method.lower())(
                f"{self.base_url}{path}", headers=headers, json=data
            ) as resp:
                # Handle rate limiting with retry
                if resp.status == 429:
                    retry_after = int(resp.headers.get("Retry-After", 5))
                    logger.warning(f"Rate limited on {method} {path}, retry after {retry_after}s")
                    self._error_count += 1
                    raise KalshiRateLimitError(retry_after)

                # Handle other errors — raise so callers can distinguish from empty results
                if resp.status not in (200, 201):
                    self._error_count += 1
                    body = await resp.text()
                    logger.warning(f"API error {resp.status} on {method} {path}: {body[:200]}")
                    raise KalshiAPIError(resp.status, body[:200])

                return await resp.json()

        except asyncio.TimeoutError:
            self._error_count += 1
            logger.error(f"Timeout on {method} {path}")
            raise
        except aiohttp.ClientError as e:
            self._error_count += 1
            logger.error(f"HTTP error on {method} {path}: {e}")
            raise

    async def _req_safe(self, method: str, path: str, data: dict = None, auth: bool = False) -> dict:
        """Make an API request, returning empty dict on all failures (safe version)."""
        try:
            return await self._req(method, path, data, auth)
        except Exception as e:
            logger.error(f"Request failed after retries: {method} {path} - {e}")
            return {}

    # =========================================================================
    # Public API Methods
    # =========================================================================

    async def get_markets(self, series_ticker: str = None, status: str = "open", limit: int = 100) -> list:
        """Get list of markets, optionally filtered by series ticker."""
        params = [f"limit={limit}"]
        if series_ticker:
            params.append(f"series_ticker={series_ticker}")
        if status:
            params.append(f"status={status}")
        result = await self._req_safe("GET", f"/markets?{'&'.join(params)}")
        return result.get("markets", [])

    async def get_orderbook(self, ticker: str, depth: int = ORDERBOOK_DEPTH) -> dict:
        """Get orderbook for a market."""
        result = await self._req_safe("GET", f"/markets/{ticker}/orderbook?depth={depth}")
        return result.get("orderbook", {})

    # =========================================================================
    # Authenticated API Methods
    # =========================================================================

    async def get_balance(self) -> float:
        """Get account balance in dollars."""
        result = await self._req_safe("GET", "/portfolio/balance", auth=True)
        return result.get("balance", 0) / 100.0

    async def get_positions(self) -> list:
        """Get all open positions."""
        result = await self._req_safe("GET", "/portfolio/positions", auth=True)
        return result.get("market_positions", [])

    async def get_fills(self, ticker: str = None, limit: int = 200) -> list:
        """Get fill history."""
        path = f"/portfolio/fills?limit={limit}"
        if ticker:
            path += f"&ticker={ticker}"
        result = await self._req_safe("GET", path, auth=True)
        return result.get("fills", [])

    async def place_order(
        self,
        ticker: str,
        side: str,
        action: str,
        count: int,
        price: int,
        order_type: str = "limit",
        client_order_id: Optional[str] = None,
    ) -> dict:
        """Place an order. Returns order details or empty dict on failure.

        client_order_id is a UUID generated by the caller *before* calling
        this method. Kalshi deduplicates on this key within a 24-hour window,
        preventing double-fills when a network timeout triggers a retry.
        """
        idempotency_key = client_order_id or str(uuid.uuid4())
        data = {
            "ticker": ticker,
            "side": side,
            "action": action,
            "count": count,
            "type": order_type,
            "client_order_id": idempotency_key,
        }
        if order_type == "limit":
            data["yes_price" if side == "yes" else "no_price"] = price

        logger.info("Placing order: %s %s %dx %s @ %dc (cid=%s)", side, action, count, ticker, price, idempotency_key)
        return await self._req_safe("POST", "/portfolio/orders", data, auth=True)

    async def get_orders(self, ticker: str = None, status: str = "resting") -> list:
        """Get open orders."""
        path = f"/portfolio/orders?status={status}"
        if ticker:
            path += f"&ticker={ticker}"
        result = await self._req_safe("GET", path, auth=True)
        return result.get("orders", [])

    async def cancel_order(self, order_id: str) -> dict:
        """Cancel an order by ID."""
        logger.info(f"Canceling order: {order_id}")
        return await self._req_safe("DELETE", f"/portfolio/orders/{order_id}", auth=True)


async def fetch_balance_quick() -> float:
    """Convenience: connect, fetch balance, disconnect. Returns 0.0 on failure."""
    import os
    api_key = os.getenv("KALSHI_API_KEY_ID")
    pk_path = os.getenv("KALSHI_PRIVATE_KEY_PATH")
    if not api_key or not pk_path:
        return 0.0
    client = KalshiClient(api_key_id=api_key, private_key_path=pk_path, demo_mode=False)
    try:
        await client.start()
        return await client.get_balance()
    except Exception as e:
        logger.warning(f"Balance fetch failed: {e}")
        return 0.0
    finally:
        await client.stop()
