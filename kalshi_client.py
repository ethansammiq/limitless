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

__all__ = [
    "KalshiClient",
    "KalshiAPIError",
    "KalshiRateLimitError",
    "fetch_balance_quick",
    "parse_fp",
    "parse_dollars",
    "normalize_market",
    "normalize_order",
    "normalize_orderbook",
]


def parse_fp(value, default: int = 0) -> int:
    """Parse a Kalshi fixed-point contract field to a whole-contract int.

    Kalshi removed the legacy integer ``position``/``count`` fields on
    2026-03-12, replacing them with fixed-point STRING fields suffixed ``_fp``
    ("13.00" == 13 contracts, 0.01-contract granularity). This bot trades whole
    contracts, so we round to the nearest int. Tolerates the legacy int / None
    form too, so callers stay correct across the rollout and for the
    PaperBroker mirror.
    """
    if value is None:
        return default
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return default


def parse_dollars(value, default: float = 0.0) -> float:
    """Parse a Kalshi ``_dollars`` money string to a float ("12.34" -> 12.34)."""
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


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


def normalize_market(mkt: dict) -> dict:
    """Backfill the legacy integer market fields from Kalshi's post-2026-03-12
    fixed-point fields, in place, and return the dict.

    Kalshi removed the integer fields (yes_bid, yes_ask, volume, ...) and now
    returns yes_bid_dollars ('0.09'), volume_fp ('27578.07'), etc. Much of the
    codebase still reads the old keys; rather than patch every call site, every
    entry point that hands back raw market dicts runs them through this so
    consumers keep working. Idempotent: a legacy key is only filled when it is
    absent/None and a fixed-point source exists, so real integers (and genuine
    zeros) are preserved.
    """
    if not isinstance(mkt, dict):
        return mkt
    for legacy, src in (
        ("yes_bid", "yes_bid_dollars"), ("yes_ask", "yes_ask_dollars"),
        ("no_bid", "no_bid_dollars"), ("no_ask", "no_ask_dollars"),
        ("last_price", "last_price_dollars"), ("previous_price", "previous_price_dollars"),
    ):
        if mkt.get(legacy) is None and mkt.get(src) is not None:
            mkt[legacy] = round(parse_dollars(mkt[src]) * 100)
    for legacy, src in (
        ("volume", "volume_fp"), ("volume_24h", "volume_24h_fp"),
        ("open_interest", "open_interest_fp"),
    ):
        if mkt.get(legacy) is None and mkt.get(src) is not None:
            mkt[legacy] = parse_fp(mkt[src])
    return mkt


def normalize_orderbook(raw: dict) -> dict:
    """Return a Kalshi orderbook in the legacy ``{"yes": [[cents, qty], ...],
    "no": [...]}`` shape, regardless of API version.

    The 2026-03-12 fixed-point migration replaced the ``orderbook`` key
    (integer-cent prices, integer quantities) with ``orderbook_fp`` whose
    ``yes_dollars``/``no_dollars`` levels are ['0.4000', '35.15'] — dollar-string
    price, fixed-point-string size. Every consumer (PaperBroker fill sim,
    edge_scanner depth map, get_orderbook callers) still reads the old ``yes``/
    ``no`` integer shape, so a raw response silently parsed to an EMPTY book —
    the root cause of the 0% fill rate. Idempotent across both shapes.
    """
    if not isinstance(raw, dict):
        return {"yes": [], "no": []}

    legacy = raw.get("orderbook")
    if isinstance(legacy, dict) and (legacy.get("yes") is not None or legacy.get("no") is not None):
        return {"yes": legacy.get("yes") or [], "no": legacy.get("no") or []}

    fp = raw.get("orderbook_fp")
    if isinstance(fp, dict):
        def _levels(rows):
            out = []
            for lv in rows or []:
                if not isinstance(lv, (list, tuple)) or len(lv) < 2:
                    continue
                cents = round(parse_dollars(lv[0]) * 100)
                qty = parse_fp(lv[1])
                if qty > 0:
                    out.append([cents, qty])
            return out
        return {"yes": _levels(fp.get("yes_dollars")), "no": _levels(fp.get("no_dollars"))}

    # Some responses nest the book at the top level under the version-specific key
    return {"yes": raw.get("yes") or [], "no": raw.get("no") or []}


def normalize_order(order: dict) -> dict:
    """Backfill the legacy V1 order fields on a V2 order object, in place.

    V2 order objects (GET /portfolio/orders) express direction as
    ``outcome_side`` ("yes"/"no") + ``book_side`` ("bid"/"ask", YES-book
    perspective) — the legacy ``side``/``action`` pair is deprecated, slated
    for removal after 2026-05-14 — and money/size as ``_dollars``/``_fp``
    strings. position_monitor filters resting orders on ``action == "buy"``,
    so once Kalshi drops the legacy fields the bot-window protection would
    silently skip every order without this. Idempotent: legacy keys are only
    filled when absent, so PaperBroker orders (born with the V1 shape) pass
    through untouched.
    """
    if not isinstance(order, dict):
        return order
    outcome = order.get("outcome_side")
    book = order.get("book_side")
    if order.get("side") is None and outcome in ("yes", "no"):
        order["side"] = outcome
    if order.get("action") is None and outcome in ("yes", "no") and book in ("bid", "ask"):
        # Bid on YES or ask on NO acquires the outcome; the diagonal exits it.
        order["action"] = "buy" if (book == "bid") == (outcome == "yes") else "sell"
    for legacy, src in (("yes_price", "yes_price_dollars"), ("no_price", "no_price_dollars")):
        if order.get(legacy) is None and order.get(src) is not None:
            order[legacy] = round(parse_dollars(order[src]) * 100)
    for legacy, src in (
        ("fill_count", "fill_count_fp"),
        ("remaining_count", "remaining_count_fp"),
        ("initial_count", "initial_count_fp"),
    ):
        if order.get(legacy) is None and order.get(src) is not None:
            order[legacy] = parse_fp(order[src])
    return order


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
        return [normalize_market(m) for m in result.get("markets", [])]

    async def get_orderbook(self, ticker: str, depth: int = ORDERBOOK_DEPTH) -> dict:
        """Get orderbook in the legacy {"yes": [[cents, qty]], "no": [...]} shape.

        Post-2026-03-12 the API returns ``orderbook_fp`` (dollar-string prices,
        fixed-point sizes); normalize_orderbook handles both shapes so callers
        see a populated book instead of an empty one.
        """
        result = await self._req_safe("GET", f"/markets/{ticker}/orderbook?depth={depth}")
        return normalize_orderbook(result)

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
        expiration_time: Optional[int] = None,
    ) -> dict:
        """Place an order. Returns {"order": {...}} or empty dict on failure.

        The signature keeps the V1 vocabulary (yes/no + buy/sell, integer
        cents) because every call site and PaperBroker speak it; translation
        to the V2 single-book schema happens here (POST /portfolio/orders
        410s since 2026-07, replaced by /portfolio/events/orders). V2 quotes
        everything from the YES perspective: buying YES or selling NO is a
        "bid", selling YES or buying NO is an "ask", and a NO price maps to
        100c minus itself. order_type "market" maps to immediate_or_cancel
        (V2 has no market type; price still caps the fill).

        client_order_id is a UUID generated by the caller *before* calling
        this method. Kalshi deduplicates on this key within a 24-hour window,
        preventing double-fills when a network timeout triggers a retry.

        expiration_time (unix seconds) turns the resting order into
        good-till-time; None rests until canceled.
        """
        if side not in ("yes", "no") or action not in ("buy", "sell"):
            # Fail loudly: a typo here would otherwise be silently complemented
            # into a live order at 100c minus the intended price.
            raise ValueError(f"place_order: invalid side/action {side!r}/{action!r}")
        idempotency_key = client_order_id or str(uuid.uuid4())
        bids_yes = (action == "buy") == (side == "yes")
        yes_cents = price if side == "yes" else 100 - price
        data = {
            "ticker": ticker,
            "side": "bid" if bids_yes else "ask",
            "count": f"{int(count)}.00",
            "price": f"{yes_cents / 100:.4f}",
            "time_in_force": (
                "immediate_or_cancel" if order_type == "market" else "good_till_canceled"
            ),
            "self_trade_prevention_type": "taker_at_cross",
            "client_order_id": idempotency_key,
        }
        if expiration_time is not None:
            data["expiration_time"] = int(expiration_time)

        logger.info(
            "Placing order: %s %s %dx %s @ %dc -> V2 %s @ %s (cid=%s)",
            side, action, count, ticker, price, data["side"], data["price"], idempotency_key,
        )
        res = await self._req_safe("POST", "/portfolio/events/orders", data, auth=True)
        if not res:
            return {}
        order = dict(res["order"]) if isinstance(res.get("order"), dict) else dict(res)
        if not order.get("status"):
            # The V2 create response carries only fill/remaining counts, but
            # callers gate success on status (RESTING/EXECUTED) — derive it.
            filled = parse_fp(order.get("fill_count", order.get("fill_count_fp")))
            remaining = parse_fp(order.get("remaining_count", order.get("remaining_count_fp")))
            order["status"] = "executed" if filled > 0 and remaining == 0 else "resting"
        order.setdefault("client_order_id", idempotency_key)
        order.setdefault("ticker", ticker)
        order.setdefault("side", side)
        order.setdefault("action", action)
        return {"order": order}

    async def get_orders(self, ticker: str = None, status: str = "resting") -> list:
        """Get open orders, normalized to the legacy field vocabulary.

        Reads stayed on GET /portfolio/orders in the V2 migration — only the
        write endpoints moved under /portfolio/events/ (GET there 404s). The
        order objects themselves are V2-shaped, so normalize_order backfills
        the deprecated side/action and integer price/count fields.
        """
        path = f"/portfolio/orders?status={status}"
        if ticker:
            path += f"&ticker={ticker}"
        result = await self._req_safe("GET", path, auth=True)
        return [normalize_order(o) for o in result.get("orders", [])]

    async def cancel_order(self, order_id: str) -> dict:
        """Cancel an order by ID (V2 endpoint; the V1 DELETE path 410s).

        V2 responds with {order_id, client_order_id, reduced_by, ts_ms}
        rather than the V1 {"order": {...}} wrapper; callers only check
        truthiness, so the body is returned as-is.
        """
        logger.info(f"Canceling order: {order_id}")
        return await self._req_safe("DELETE", f"/portfolio/events/orders/{order_id}", auth=True)


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
