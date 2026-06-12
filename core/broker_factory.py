"""Factory that returns a live or paper broker based on PAPER_TRADING_MODE.

Callers that previously wrote:
    client = KalshiClient(api_key_id=..., private_key_path=..., demo_mode=False)
    await client.start()

should write instead:
    broker = await get_broker()

and use broker.get_balance(), broker.place_order(), etc. exactly as before.
"""

from __future__ import annotations

import os
from typing import Optional

from log_setup import get_logger
from config import PAPER_TRADING_MODE
from core.broker import BrokerInterface, KalshiBroker, PaperBroker
from kalshi_client import KalshiClient

logger = get_logger(__name__)

__all__ = ["get_broker"]


async def get_broker(
    paper: Optional[bool] = None,
    demo_mode: bool = False,
) -> BrokerInterface:
    """Return a started BrokerInterface appropriate for the active mode.

    Args:
        paper: Override for PAPER_TRADING_MODE. If None, reads the env var.
        demo_mode: Use Kalshi demo endpoint (only meaningful for live mode or
                   when PaperBroker's quote source should hit demo).

    Returns:
        A BrokerInterface whose start() has already completed.

    Raises:
        RuntimeError: if live mode is requested without credentials.
    """
    use_paper = paper if paper is not None else PAPER_TRADING_MODE

    if use_paper:
        # PaperBroker needs a KalshiClient for quote data. It does NOT need
        # credentials — get_markets and get_orderbook are public endpoints.
        quote_client = KalshiClient(demo_mode=demo_mode)
        broker = PaperBroker(quote_client=quote_client)
        await broker.start()
        bal = await broker.get_balance()
        logger.info("Broker: PAPER | fill_mode=%s | balance=$%.2f", broker._fill_mode, bal)
        return broker

    api_key = os.getenv("KALSHI_API_KEY_ID")
    pk_path = os.getenv("KALSHI_PRIVATE_KEY_PATH")
    if not api_key or not pk_path:
        raise RuntimeError(
            "Live broker requires KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH "
            "in the environment. Set PAPER_TRADING_MODE=true to use PaperBroker instead."
        )
    broker = KalshiBroker(api_key_id=api_key, private_key_path=pk_path, demo_mode=demo_mode)
    await broker.start()
    logger.info("Broker: LIVE%s", " (demo endpoint)" if demo_mode else "")
    return broker
