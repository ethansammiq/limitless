#!/usr/bin/env python3
"""FUNDING LOGGER — Kalshi perps funding vs Coinbase spot basis capture.

Kalshi launched crypto perpetual futures 2026-05-29 (public margin API, no
auth). The open research question before any capital touches them: is Kalshi
funding persistently rich or cheap vs spot carry? Early history shows BTC/ETH
funding pinned at 0 while alt perps (HYPE/SUI/DOGE/LTC/NEAR) print consistently
NEGATIVE funding (~-0.01 to -0.02%/8h, longs receive). This logger builds the
dataset that answers whether that persists — capture only, zero capital,
alert-nothing.

Two append-only journals under logs/funding/:
  YYYY-MM-DD.jsonl  per-active-market snapshots: perp bid/ask/mid, Kalshi
                    reference (index) price, live funding-rate estimate, next
                    funding time, Coinbase spot, and mid-vs-spot basis in bps.
  prints.jsonl      settled 8h funding prints (04/12/20 UTC) pulled from
                    /margin/funding_rates/historical, deduped against the
                    journal itself (the journal is the state — no state file).
                    First run backfills the full history since launch.

Rows are timestamped at fetch time, not run start (shadow_logger lesson).
Heartbeat "funding_logger" on every clean exit.

Usage:
    python3 funding_logger.py --once            # cron entry point
    python3 funding_logger.py --once --dry-run  # print instead of write

Suggested crontab (NOT auto-installed):
    */10 * * * * $VENV $PROJ/funding_logger.py --once >> /tmp/funding_logger.log 2>&1
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from heartbeat import write_heartbeat
from log_setup import get_logger

logger = get_logger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent
OUT_DIR = PROJECT_ROOT / "logs" / "funding"
PRINTS_FILE = OUT_DIR / "prints.jsonl"

# Perps live on their own host, separate from api.elections.kalshi.com; the
# market-data and funding endpoints are unauthenticated (perps_openapi.yaml).
PERPS_URL = "https://external-api.kalshi.com/trade-api/v2"
COINBASE_URL = "https://api.coinbase.com/v2/prices"

REQUEST_SPACING_SEC = 0.15
# Incremental print pulls re-fetch a trailing window and dedupe, so a run can
# gap up to this many days (watchdog alerts at 2h) without losing prints.
PRINTS_LOOKBACK_DAYS = 5


def perp_symbol(ticker: str) -> str:
    """KX{SYM}PERP -> Coinbase spot symbol (kSHIB settles on SHIB)."""
    sym = ticker.removeprefix("KX").removesuffix("PERP")
    return "SHIB" if sym == "KSHIB" else sym


def units_per_contract_unit(ticker: str) -> float:
    """Coinbase units per ONE Kalshi contract-size unit. Kalshi denominates
    KXKSHIBPERP in kSHIB (contract_size=1000 means 1000 kSHIB = 1M SHIB);
    everything else trades in the spot unit directly. Verified live
    2026-07-09: naive mid/contract_size put kSHIB basis at +9.9M bps."""
    return 1000.0 if ticker == "KXKSHIBPERP" else 1.0


def coinbase_product(ticker: str) -> str:
    return f"{perp_symbol(ticker)}-USD"


def _f(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def build_snapshot_row(market: dict, estimate: dict | None,
                       cb_spot: float | None, ts: str) -> dict:
    """One journal row per active market: perp quote + funding + spot basis.

    Prices are dollars per CONTRACT; dividing by contract_size (and the
    kSHIB unit scale) gives the implied price in Coinbase-product units,
    directly comparable to cb_spot. Positive basis_bps = perp rich to spot.
    """
    bid, ask = _f(market.get("bid")), _f(market.get("ask"))
    mid = (bid + ask) / 2 if bid is not None and ask is not None else None
    cs = _f(market.get("contract_size"))
    scale = units_per_contract_unit(market.get("ticker", ""))
    implied = mid / (cs * scale) if mid is not None and cs else None
    basis_bps = (
        round((implied - cb_spot) / cb_spot * 1e4, 2)
        if implied is not None and cb_spot else None
    )
    ref = (market.get("reference_price") or {}).get("price")
    return {
        "ts": ts,
        "ticker": market.get("ticker"),
        "sym": perp_symbol(market.get("ticker", "")),
        "bid": bid,
        "ask": ask,
        "mid": mid,
        "contract_size": cs,
        "ref_price": _f(ref),
        "funding_est": _f((estimate or {}).get("funding_rate")),
        "next_funding_time": (estimate or {}).get("next_funding_time"),
        "cb_spot": cb_spot,
        "implied_spot": round(implied, 8) if implied is not None else None,
        "basis_bps": basis_bps,
        "oi_usd": _f(market.get("open_interest_notional_value_dollars")),
        "vol24h_usd": _f(market.get("volume_24h_notional_value_dollars")),
    }


def dedupe_prints(fetched: list[dict], existing_keys: set[tuple[str, str]]) -> list[dict]:
    """Keep only funding prints not already journaled, oldest first."""
    fresh = [
        p for p in fetched
        if (p.get("market_ticker"), p.get("funding_time")) not in existing_keys
    ]
    return sorted(fresh, key=lambda p: (p.get("funding_time") or "", p.get("market_ticker") or ""))


def _get_json(url: str, retries: int = 3):
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "WeatherEdgeFunding/1.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            if exc.code == 404:  # unlisted Coinbase product — not a retry case
                raise
            last = exc
        except Exception as exc:  # noqa: BLE001 — network, retry
            last = exc
        time.sleep(1.0 * (attempt + 1))
    raise RuntimeError(f"fetch failed: {url[:100]} ({last})")


def fetch_coinbase_spot(product: str) -> float | None:
    """Per-unit USD spot; None when Coinbase doesn't list it (e.g. HYPE)."""
    try:
        return _f(_get_json(f"{COINBASE_URL}/{product}/spot")["data"]["amount"])
    except urllib.error.HTTPError as exc:
        logger.info(f"coinbase {product}: HTTP {exc.code} (unlisted?)")
        return None
    except (RuntimeError, KeyError, TypeError) as exc:
        logger.warning(f"coinbase {product}: {exc}")
        return None


def capture_snapshots() -> list[dict]:
    markets = _get_json(f"{PERPS_URL}/margin/markets").get("markets", [])
    active = [m for m in markets if m.get("status") == "active"]

    spots: dict[str, float | None] = {}
    rows: list[dict] = []
    for mkt in active:
        ticker = mkt.get("ticker", "")
        product = coinbase_product(ticker)
        if product not in spots:
            spots[product] = fetch_coinbase_spot(product)
        try:
            time.sleep(REQUEST_SPACING_SEC)
            est = _get_json(f"{PERPS_URL}/margin/funding_rates/estimate?ticker={ticker}")
        except RuntimeError as exc:  # one market must not kill the run
            logger.warning(f"{ticker}: funding estimate failed: {exc}")
            est = None
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        rows.append(build_snapshot_row(mkt, est, spots[product], ts))
    return rows


def _journaled_print_keys() -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    try:
        with PRINTS_FILE.open() as fh:
            for line in fh:
                try:
                    p = json.loads(line)
                    keys.add((p.get("market_ticker"), p.get("funding_time")))
                except json.JSONDecodeError:
                    continue
    except FileNotFoundError:
        pass
    return keys


def capture_prints(now_utc: datetime) -> list[dict]:
    """New settled funding prints across all markets, deduped vs the journal."""
    existing = _journaled_print_keys()
    url = f"{PERPS_URL}/margin/funding_rates/historical"
    if existing:  # incremental: trailing window is enough
        start_ts = int((now_utc - timedelta(days=PRINTS_LOOKBACK_DAYS)).timestamp())
        url += f"?start_ts={start_ts}"
    # else: first run — no start_ts backfills the full history since launch
    fetched = _get_json(url).get("funding_rates", [])
    fresh = dedupe_prints(fetched, existing)
    captured_ts = now_utc.isoformat(timespec="seconds")
    return [{**p, "captured_ts": captured_ts} for p in fresh]


def append_jsonl(path: Path, rows: list[dict]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        for row in rows:
            fh.write(json.dumps(row, separators=(",", ":")) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--once", action="store_true", help="single capture (cron mode)")
    ap.add_argument("--dry-run", action="store_true", help="print instead of write")
    args = ap.parse_args()
    if not args.once:
        ap.error("only --once mode is supported; schedule via cron")

    now_utc = datetime.now(timezone.utc)
    snapshots = capture_snapshots()
    try:
        prints = capture_prints(now_utc)
    except RuntimeError as exc:  # prints re-pull next run; snapshots are the loss-sensitive part
        logger.warning(f"funding prints fetch failed: {exc}")
        prints = []

    if args.dry_run:
        for row in snapshots + prints:
            print(json.dumps(row))
        print(f"# {len(snapshots)} snapshots, {len(prints)} new prints (dry run)")
    else:
        if snapshots:
            append_jsonl(OUT_DIR / f"{now_utc.strftime('%Y-%m-%d')}.jsonl", snapshots)
        if prints:
            append_jsonl(PRINTS_FILE, prints)
        logger.info(f"funding capture: {len(snapshots)} snapshots, {len(prints)} new prints")
    write_heartbeat("funding_logger")


if __name__ == "__main__":
    main()
