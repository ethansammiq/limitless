#!/usr/bin/env python3
"""take.py — the human trigger finger. Alerts print the exact command; you run it.

    .venv/bin/python scripts/take.py TICKER buy|sell yes|no COUNT PRICE_C
        [--ioc]             take-what's-there: fill at PRICE_C or better, cancel rest
        [--expire-et HH:MM] resting limit auto-expires at this ET time today
        [--yes]             skip the confirmation prompt

Examples (the shapes the sniper/sweeper/live_watch alerts emit):
    take.py KXHIGHCHI-26JUL04-B84.5 buy  yes 40 16 --ioc
    take.py KXHIGHNY-26JUL02-T99    sell yes 20 22 --ioc

Guards: price 1-99; notional (COUNT x PRICE, or the worst-case collateral on
sells/no-sides) capped at $50 unless TAKE_MAX_NOTIONAL says otherwise; a
y/N confirmation unless --yes. This is the ONLY order-placing entry point —
automated jobs alert, humans execute.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

DEFAULT_MAX_NOTIONAL = 50.0
ET = ZoneInfo("America/New_York")


def order_cost_dollars(action: str, side: str, count: int, price_c: int) -> float:
    """Worst-case collateral: buys cost price; sells of YES you hold cost 0
    but selling short / buying NO collateralizes the complement. Be
    conservative and cap on the larger leg."""
    leg = price_c if action == "buy" else 100 - price_c
    return count * leg / 100


def validate(args, max_notional: float) -> str | None:
    if not (1 <= args.price_c <= 99):
        return f"price {args.price_c}c outside 1-99"
    if args.count < 1:
        return "count must be >= 1"
    cost = order_cost_dollars(args.action, args.side, args.count, args.price_c)
    if cost > max_notional:
        return (f"worst-case collateral ${cost:.2f} exceeds cap "
                f"${max_notional:.2f} (raise TAKE_MAX_NOTIONAL to override)")
    return None


def expire_ts(hhmm: str) -> int:
    hour, minute = (int(x) for x in hhmm.split(":"))
    now = datetime.now(ET)
    return int(now.replace(hour=hour, minute=minute, second=0, microsecond=0).timestamp())


def summarize_order_fills(fills: list[dict], order_id: str) -> tuple[int, float]:
    """(contracts filled, avg yes-price in cents) for one order's fills.

    The instant place_order response can say status=resting even for a
    market/IOC order that is already cancelling (observed live 2026-07-10:
    two IOC buys reported "resting", filled nothing, and the truth only
    surfaced via the account snapshot 10 minutes later). Fills are the
    ground truth; report from them."""
    total = 0
    cents_qty = 0.0
    for f in fills or []:
        if f.get("order_id") != order_id:
            continue
        qty = float(f.get("count_fp") or f.get("count") or 0)
        yes_c = f.get("yes_price")
        if yes_c is None:
            yes_c = round(float(f.get("yes_price_dollars") or 0) * 100)
        total += int(round(qty))
        cents_qty += qty * yes_c
    return total, (cents_qty / total if total else 0.0)


async def run(args) -> None:
    from kalshi_client import KalshiClient

    client = KalshiClient(
        api_key_id=os.getenv("KALSHI_API_KEY_ID", ""),
        private_key_path=os.getenv("KALSHI_PRIVATE_KEY_PATH", ""),
        demo_mode=False,
    )
    await client.start()
    try:
        print(f"balance before: ${await client.get_balance():.2f}")
        result = await client.place_order(
            ticker=args.ticker,
            side=args.side,
            action=args.action,
            count=args.count,
            price=args.price_c,
            order_type="market" if args.ioc else "limit",
            client_order_id=str(uuid.uuid4()),
            expiration_time=expire_ts(args.expire_et) if args.expire_et else None,
        )
        order = (result or {}).get("order") or {}
        order_id = order.get("order_id", "")
        print(f"order {order_id or 'FAILED'} status={order.get('status')}")
        if order_id:
            await asyncio.sleep(1.5)  # let the match/cancel settle
            fills = await client.get_fills(ticker=args.ticker)
            filled, avg_c = summarize_order_fills(fills, order_id)
            if filled:
                print(f"FILLED {filled}/{args.count} @ avg {avg_c:.1f}c")
            else:
                print(f"FILLED 0/{args.count} — no fills for this order "
                      f"(IOC found no book at {args.price_c}c or better)"
                      if args.ioc else
                      f"FILLED 0/{args.count} — order resting on the book")
        for p in await client.get_positions() or []:
            if p.get("ticker") == args.ticker:
                print(f"position: {p.get('position_fp')} "
                      f"(realized ${p.get('realized_pnl_dollars')})")
        print(f"balance after: ${await client.get_balance():.2f}")
    finally:
        await client.stop()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("ticker")
    ap.add_argument("action", choices=("buy", "sell"))
    ap.add_argument("side", choices=("yes", "no"))
    ap.add_argument("count", type=int)
    ap.add_argument("price_c", type=int, help="limit price in cents (1-99)")
    ap.add_argument("--ioc", action="store_true",
                    help="immediate-or-cancel at price or better")
    ap.add_argument("--expire-et", metavar="HH:MM",
                    help="resting order auto-expires at this ET time today")
    ap.add_argument("--yes", action="store_true", help="skip confirmation")
    args = ap.parse_args()

    max_notional = float(os.getenv("TAKE_MAX_NOTIONAL", DEFAULT_MAX_NOTIONAL))
    problem = validate(args, max_notional)
    if problem:
        ap.error(problem)

    cost = order_cost_dollars(args.action, args.side, args.count, args.price_c)
    mode = "IOC" if args.ioc else (f"GTT->{args.expire_et} ET" if args.expire_et else "GTC")
    print(f"{args.action.upper()} {args.side.upper()} {args.count}x "
          f"{args.ticker} @ {args.price_c}c [{mode}] — worst-case ${cost:.2f}")
    if not args.yes and input("place LIVE order? [y/N] ").strip().lower() != "y":
        raise SystemExit("aborted")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
