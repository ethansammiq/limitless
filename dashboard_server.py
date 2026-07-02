#!/usr/bin/env python3
"""Live dashboard server for Weather Edge.

Read-only aiohttp server that serves dashboard.html and exposes the bot's
live state files as JSON, so the dashboard can auto-refresh and show the
*actual* paper-trading state (balance, P&L, open positions, recent orders,
service heartbeats, peak monitor) instead of a one-shot static load.

Zero new dependencies — aiohttp is already a project requirement.

SECURITY: binds to localhost only. This surfaces trading/account state and
must never be reachable off-machine. Do not expose it; it loads no secrets
and never touches the live Kalshi account (it only reads on-disk JSON state).

Run:
    .venv/bin/python3 dashboard_server.py
    # then open http://127.0.0.1:8787

Env:
    DASHBOARD_HOST   default 127.0.0.1
    DASHBOARD_PORT   default 8787
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from collections import Counter, deque
from datetime import datetime, timezone
from pathlib import Path

from aiohttp import web

ROOT = Path(__file__).resolve().parent

# Load .env BEFORE importing config — config reads PAPER_TRADING_MODE at import
# time via os.getenv, so without this it would default to LIVE and serve the
# wrong position file (mirrors position_monitor.py / auto_scan.py).
try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except Exception:
    pass

# Resolve canonical state-file paths from config (honors PAPER_TRADING_MODE);
# fall back to basenames in the repo root if config can't be imported.
try:
    import config as _cfg

    PAPER_MODE = bool(getattr(_cfg, "PAPER_TRADING_MODE", False))
    POSITIONS_FILE = Path(getattr(_cfg, "get_positions_file", lambda: ROOT / "positions_paper.json")())
    BALANCE_FILE = Path(getattr(_cfg, "PAPER_BALANCE_FILE", ROOT / "paper_balance.json"))
    ORDERS_FILE = Path(getattr(_cfg, "PAPER_ORDERS_FILE", ROOT / "paper_orders.json"))
except Exception:  # pragma: no cover - defensive fallback
    PAPER_MODE = os.getenv("PAPER_TRADING_MODE", "false").lower() in ("true", "1", "yes")
    POSITIONS_FILE = ROOT / ("positions_paper.json" if PAPER_MODE else "positions.json")
    BALANCE_FILE = ROOT / "paper_balance.json"
    ORDERS_FILE = ROOT / "paper_orders.json"

HEARTBEATS_FILE = ROOT / "heartbeats.json"
PEAKS_FILE = ROOT / "peak_state.json"
CALIBRATION_FILE = ROOT / "calibration_cache.json"
SCAN_FILE = ROOT / "scan_data.json"
DASHBOARD_HTML = ROOT / "dashboard.html"

# Recent orders to expose (paper_orders.json grows unbounded; the full file is
# ~400KB and we never want to ship that to the browser every 15s).
RECENT_ORDERS = 30

# position_monitor runs every 5 min, 24/7 — it is the true liveness signal.
LIVENESS_INTERVAL_S = 300
# scan_data.json older than this is treated as stale (don't render it as "live").
STALE_SCAN_S = 3 * 3600

# ─── Live contract-price polling ──────────────────────────────────────────────
# A background task polls Kalshi market data (read-only, no trading) for the open
# weather brackets and keeps a per-contract price time series in memory, so the
# dashboard can chart contract prices live. Reuses the bot's own KalshiClient.
PRICES_ENABLED = os.getenv("DASHBOARD_PRICES", "1").lower() not in ("0", "false", "no", "off")
PRICE_POLL_SECONDS = max(15, int(os.getenv("DASHBOARD_PRICE_POLL_SECONDS", "60")))
PRICE_TOP_N = int(os.getenv("DASHBOARD_PRICE_TOP_N", "8"))   # tracked per city, by volume
PRICE_MAX_POINTS = 480                                        # ring-buffer length per ticker
PRICE_MAX_TICKERS = 80
PRICE_HISTORY_FILE = ROOT / "price_history.json"

# Persisted "start of day" equity so the KPI feed can show today's P&L.
DAY_ANCHOR_FILE = ROOT / "dashboard_day_anchor.json"

# ─── Live temperature observations ────────────────────────────────────────────
# Each poll reuses peak_monitor.fetch_iem_observations (free IEM ASOS, NWS
# fallback), which returns today's full intraday obs curve per city — so the
# temp graph is populated immediately, not built up over the day.
TEMPS_ENABLED = os.getenv("DASHBOARD_TEMPS", "1").lower() not in ("0", "false", "no", "off")
TEMP_POLL_SECONDS = max(60, int(os.getenv("DASHBOARD_TEMP_POLL_SECONDS", "300")))
TEMP_HISTORY_FILE = ROOT / "temp_history.json"
_NUM_RE = re.compile(r"\d+(?:\.\d+)?")        # positive temps (these markets are summer highs)

# city -> {series: [{t, temp}], current, high, updated}
_temps: dict = {}
_temps_meta = {
    "enabled": TEMPS_ENABLED,
    "poll_seconds": TEMP_POLL_SECONDS,
    "updated": None,
    "last_poll_ok": None,
    "last_error": None,
}

# ticker -> {city, date, label, bid, ask, last, mid, volume, updated, points: deque}
_prices: dict = {}
_prices_meta = {
    "enabled": PRICES_ENABLED,
    "poll_seconds": PRICE_POLL_SECONDS,
    "updated": None,
    "last_poll_ok": None,
    "last_error": None,
    "cities": [],
}

_DATE_RE = re.compile(r"-(\d{2})([A-Z]{3})(\d{2})-")


def _to_float(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _cents(v):
    """Convert a Kalshi *_dollars string (e.g. '0.0900') to integer cents."""
    try:
        return round(float(v) * 100)
    except (TypeError, ValueError):
        return None


def _atomic_write(path: Path, text: str) -> None:
    """Write via temp file + os.replace so readers never see a torn file
    (matches the repo convention in position_store / peak_monitor / heartbeat)."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def _market_quote(mkt: dict):
    """Best yes bid/ask/last in cents + volume, reading the post-2026-03 fixed-point
    fields (yes_bid_dollars / volume_fp) with a fallback to the legacy integer fields."""
    bid = _cents(mkt.get("yes_bid_dollars"))
    ask = _cents(mkt.get("yes_ask_dollars"))
    last = _cents(mkt.get("last_price_dollars"))
    if bid is None:
        bid = mkt.get("yes_bid")
    if ask is None:
        ask = mkt.get("yes_ask")
    if last is None:
        last = mkt.get("last_price")
    # get(key, default) only falls back when the key is ABSENT — a present
    # `volume_fp: null` would otherwise zero a real legacy volume.
    vol_raw = mkt.get("volume_fp")
    if vol_raw is None:
        vol_raw = mkt.get("volume", 0)
    vol = _to_float(vol_raw)
    return bid, ask, last, vol


def _mid(bid, ask, last):
    # A genuine 0c quote is valid (no resting buyers) — only None means absent.
    if bid is not None and ask is not None:
        return round((bid + ask) / 2, 1)
    if last is not None:
        return float(last)
    for v in (bid, ask):
        if v is not None:
            return float(v)
    return None


def _label_date(ticker: str):
    m = _DATE_RE.search(ticker or "")
    if not m:
        return None
    _, mon, dd = m.groups()
    return f"{mon.title()} {int(dd)}"


def _ingest(city: str, markets: list, now_iso: str, held: set | None = None) -> None:
    held = held or set()
    scored = []
    for m in markets or []:
        t = m.get("ticker")
        if not t:
            continue
        bid, ask, last, vol = _market_quote(m)
        scored.append((vol, t, m, bid, ask, last))
    scored.sort(key=lambda x: x[0], reverse=True)
    # Keep the top-N by volume per city, PLUS any ticker we hold a position in,
    # so our own contracts are always charted even when they're illiquid.
    keep = scored[:PRICE_TOP_N] + [s for s in scored[PRICE_TOP_N:] if s[1] in held]
    for vol, t, m, bid, ask, last in keep:
        mid = _mid(bid, ask, last)
        if mid is None:
            continue
        rec = _prices.get(t)
        if rec is None:
            rec = _prices[t] = {
                "city": city,
                "date": _label_date(t),
                "label": (m.get("yes_sub_title") or m.get("subtitle") or "").strip(),
                "points": deque(maxlen=PRICE_MAX_POINTS),
            }
        rec.update(bid=bid, ask=ask, last=last, mid=mid, volume=vol,
                   updated=now_iso, held=(t in held))
        rec["points"].append({"t": now_iso, "mid": mid, "bid": bid, "ask": ask})


def _held_tickers() -> set:
    """Tickers we currently hold a position in (open / pending_sell / resting)."""
    positions, _ = _read_json(POSITIONS_FILE)
    return {
        p.get("ticker")
        for p in (positions or [])
        if p.get("ticker") and p.get("status") in ("open", "pending_sell", "resting")
    }


def _evict() -> None:
    if len(_prices) <= PRICE_MAX_TICKERS:
        return
    # Drop the least-recently-updated tickers.
    ranked = sorted(_prices.items(), key=lambda kv: kv[1].get("updated") or "")
    for t, _ in ranked[: len(_prices) - PRICE_MAX_TICKERS]:
        _prices.pop(t, None)


def _persist() -> None:
    try:
        out = {
            "saved": _now().isoformat(),
            "tickers": {
                t: {
                    "city": r["city"], "date": r.get("date"), "label": r.get("label"),
                    "points": list(r["points"])[-PRICE_MAX_POINTS:],
                }
                for t, r in _prices.items()
            },
        }
        _atomic_write(PRICE_HISTORY_FILE, json.dumps(out, separators=(",", ":")))
    except Exception:
        pass


def _load_history() -> None:
    data, err = _read_json(PRICE_HISTORY_FILE)
    if err or not data:
        return
    for t, r in (data.get("tickers") or {}).items():
        dq = deque(r.get("points") or [], maxlen=PRICE_MAX_POINTS)
        last = dq[-1] if dq else {}
        _prices[t] = {
            "city": r.get("city"), "date": r.get("date"), "label": r.get("label"),
            "bid": last.get("bid"), "ask": last.get("ask"), "last": None,
            "mid": last.get("mid"), "volume": 0, "updated": last.get("t"), "points": dq,
        }


async def poll_prices_once() -> None:
    from kalshi_client import KalshiClient
    from config import STATIONS

    # Public market data only — pass NO credentials so the live signing key is
    # never loaded into this read-only dashboard process (honors the module's
    # "loads no secrets" contract). get_markets() is unauthenticated.
    client = KalshiClient(api_key_id="", private_key_path="", demo_mode=False)
    now_iso = _now().isoformat()
    cities = list(STATIONS.keys())
    held = _held_tickers()
    # start() lives inside the try so a failure still runs stop() (no session leak).
    try:
        await client.start()
        for city in cities:
            try:
                mk = await client.get_markets(
                    series_ticker=STATIONS[city].series_ticker, status="open", limit=100
                )
            except Exception:
                continue
            _ingest(city, mk, now_iso, held)
    finally:
        await client.stop()
    _evict()
    _persist()
    _prices_meta.update(updated=now_iso, last_poll_ok=True, last_error=None, cities=cities)


async def price_poller_loop() -> None:
    _load_history()
    while True:
        try:
            await poll_prices_once()
        except Exception as exc:
            _prices_meta.update(last_poll_ok=False, last_error=f"{type(exc).__name__}: {exc}")
        await asyncio.sleep(PRICE_POLL_SECONDS)


def _bracket_bounds(label, ticker):
    """(lo, hi) °F for a contract's bracket, parsed from its human label
    ('66° or below' -> (None,66); '73° to 74°' -> (73,74); '82 or above' ->
    (82,None)), falling back to the ticker suffix (-T74 / -B36.5 -> point)."""
    s = (label or "").lower()
    nums = [float(x) for x in _NUM_RE.findall(s)]
    if nums:
        if any(w in s for w in ("below", "under", "or less")):
            return None, nums[0]
        if any(w in s for w in ("above", "over", "or more", "greater")):
            return nums[0], None
        if len(nums) >= 2:
            return min(nums[:2]), max(nums[:2])
        return nums[0], nums[0]
    m = re.search(r"-[TB](\d+(?:\.\d+)?)$", ticker or "")
    if m:
        v = float(m.group(1))
        return v, v
    return None, None


def _persist_temps() -> None:
    try:
        _atomic_write(TEMP_HISTORY_FILE,
                      json.dumps({"saved": _now().isoformat(), "cities": _temps}, separators=(",", ":")))
    except Exception:
        pass


def _load_temps() -> None:
    data, err = _read_json(TEMP_HISTORY_FILE)
    if err or not data:
        return
    cities = data.get("cities")
    if isinstance(cities, dict):
        _temps.update(cities)


async def poll_temps_once() -> None:
    import aiohttp
    from peak_monitor import fetch_iem_observations, fetch_today_observations
    from config import STATIONS

    now_iso = _now().isoformat()
    ok = 0
    async with aiohttp.ClientSession() as session:
        for city in STATIONS.keys():
            try:
                obs = await fetch_iem_observations(session, city)
                if not obs:
                    obs = await fetch_today_observations(session, city)
            except Exception:
                continue
            if not obs:
                continue
            series = [{"t": o.timestamp.isoformat(), "temp": o.temp_f} for o in obs]
            _temps[city] = {
                "series": series,
                "current": series[-1]["temp"] if series else None,
                "high": max((p["temp"] for p in series), default=None),
                "updated": now_iso,
            }
            ok += 1
    _persist_temps()
    # last_poll_ok reflects real success — every fetcher swallows its own errors
    # and returns [], so a total outage would otherwise look healthy.
    _temps_meta.update(updated=now_iso, last_poll_ok=(ok > 0),
                       last_error=None if ok else "no city returned observations")


async def temp_poller_loop() -> None:
    _load_temps()
    while True:
        try:
            await poll_temps_once()
        except Exception as exc:
            _temps_meta.update(last_poll_ok=False, last_error=f"{type(exc).__name__}: {exc}")
        await asyncio.sleep(TEMP_POLL_SECONDS)


async def _start_poller(app: web.Application) -> None:
    if PRICES_ENABLED:
        app["price_task"] = asyncio.create_task(price_poller_loop())
    if TEMPS_ENABLED:
        app["temp_task"] = asyncio.create_task(temp_poller_loop())


async def _stop_poller(app: web.Application) -> None:
    for key in ("price_task", "temp_task"):
        task = app.get(key)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


def _now():
    return datetime.now(timezone.utc).astimezone()


def _file_meta(path: Path) -> dict:
    if not path.exists():
        return {"exists": False, "age_seconds": None, "mtime": None}
    mtime = path.stat().st_mtime
    return {
        "exists": True,
        "age_seconds": round(time.time() - mtime, 1),
        "mtime": datetime.fromtimestamp(mtime, timezone.utc).astimezone().isoformat(),
    }


def _read_json(path: Path):
    try:
        with path.open() as fh:
            return json.load(fh), None
    except FileNotFoundError:
        return None, "missing"
    except Exception as exc:  # malformed / mid-write
        return None, f"{type(exc).__name__}: {exc}"


def _age(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        return round((_now() - datetime.fromisoformat(iso)).total_seconds(), 1)
    except Exception:
        return None


def _sellable_cents(ticker: str, side: str):
    """Price (cents) we could sell our side at right now, from the live marks.

    A YES position is sold at the YES bid; a NO position at (100 - YES ask).
    Returns None when there is no live mark for the ticker (graceful degrade).
    """
    rec = _prices.get(ticker)
    if not rec:
        return None
    yes_bid, yes_ask = rec.get("bid"), rec.get("ask")
    if side == "no":
        return (100 - yes_ask) if yes_ask is not None else None
    return yes_bid if yes_bid is not None else None


def _daily_anchor(equity: float):
    """Equity at the first observation today; resets at local midnight."""
    today = _now().date().isoformat()
    data, _ = _read_json(DAY_ANCHOR_FILE)
    if isinstance(data, dict) and data.get("date") == today and data.get("equity") is not None:
        return _to_float(data.get("equity"), None)
    try:
        _atomic_write(DAY_ANCHOR_FILE, json.dumps({"date": today, "equity": round(equity, 2)}))
    except Exception:
        pass
    return round(equity, 2)


def build_state() -> dict:
    files = {}

    balance, berr = _read_json(BALANCE_FILE)
    files["balance"] = {**_file_meta(BALANCE_FILE), "error": berr}

    positions, perr = _read_json(POSITIONS_FILE)
    files["positions"] = {**_file_meta(POSITIONS_FILE), "error": perr}
    positions = positions or []
    open_pos = [p for p in positions if p.get("status") == "open"]
    total_realized = round(sum(float(p.get("pnl_realized", 0) or 0) for p in positions), 2)
    open_contracts = sum(int(p.get("contracts", 0) or 0) for p in open_pos)
    open_cost = round(
        sum(max(float(p.get("avg_price", 0) or 0), 0) * int(p.get("contracts", 0) or 0) for p in open_pos) / 100.0,
        2,
    )

    orders, oerr = _read_json(ORDERS_FILE)
    files["orders"] = {**_file_meta(ORDERS_FILE), "error": oerr}
    orders = orders or []
    recent_orders = list(reversed(orders[-RECENT_ORDERS:]))
    order_counts = dict(Counter(o.get("status", "?") for o in orders))

    heartbeats_raw, herr = _read_json(HEARTBEATS_FILE)
    files["heartbeats"] = {**_file_meta(HEARTBEATS_FILE), "error": herr}
    heartbeats = {}
    for svc, hb in (heartbeats_raw or {}).items():
        ts = hb.get("timestamp") if isinstance(hb, dict) else None
        heartbeats[svc] = {"timestamp": ts, "age_seconds": _age(ts)}
    pm = heartbeats.get("position_monitor", {})
    pm_age = pm.get("age_seconds")
    alive = pm_age is not None and pm_age < LIVENESS_INTERVAL_S * 3  # <15 min

    peaks, pkerr = _read_json(PEAKS_FILE)
    files["peaks"] = {**_file_meta(PEAKS_FILE), "error": pkerr}

    calibration, cerr = _read_json(CALIBRATION_FILE)
    files["calibration"] = {**_file_meta(CALIBRATION_FILE), "error": cerr}

    scan, scerr = _read_json(SCAN_FILE)
    scan_meta = {**_file_meta(SCAN_FILE), "error": scerr}
    scan_age = scan_meta.get("age_seconds")
    scan_stale = scan_age is None or scan_age > STALE_SCAN_S
    scan_meta["stale"] = scan_stale
    if scan_stale:
        scan = None  # don't render stale weather as if it were live

    # ── KPI feed: equity/P&L, exposure, performance, bot health ──────────────
    cash = _to_float((balance or {}).get("balance"), 0.0)
    init = _to_float((balance or {}).get("initial_balance"), 0.0)

    # Live mark-to-market of open positions using the price poller's quotes.
    open_value = 0.0
    unrealized = 0.0
    covered = 0
    pos_rois = []  # per covered open position, for best/worst
    for p in open_pos:
        n = int(p.get("contracts", 0) or 0)
        avg = max(_to_float(p.get("avg_price"), 0.0), 0.0)
        cost = avg * n / 100.0
        sell = _sellable_cents(p.get("ticker"), p.get("side", "yes"))
        if sell is not None:
            value = sell * n / 100.0
            open_value += value
            unrealized += value - cost
            covered += 1
            if cost > 0:
                pos_rois.append({"ticker": p.get("ticker"), "roi_pct": round((value - cost) / cost * 100, 1)})
        else:
            open_value += cost  # no live mark → hold flat at cost basis
    marks_live = PRICES_ENABLED and covered > 0

    equity = round(cash + open_value, 2)
    total_pnl = round(equity - init, 2) if init > 0 else None
    total_pnl_pct = round((equity - init) / init * 100, 2) if init > 0 else None
    anchor = _daily_anchor(equity)
    daily_pnl = round(equity - anchor, 2) if anchor is not None else None
    daily_pnl_pct = round((equity - anchor) / anchor * 100, 2) if anchor else None
    deployed_pct = round(open_cost / equity * 100, 1) if equity > 0 else None

    finished = [p for p in positions if p.get("status") in ("settled", "closed")]
    wins = sum(1 for p in finished if _to_float(p.get("pnl_realized"), 0.0) > 0)
    win_rate = round(wins / len(finished) * 100, 1) if finished else None
    rois = []
    for p in finished:
        oc = int(p.get("original_contracts", p.get("contracts", 0)) or 0)
        avg = max(_to_float(p.get("avg_price"), 0.0), 0.0)
        cst = avg * oc / 100.0
        if cst > 0:
            rois.append(_to_float(p.get("pnl_realized"), 0.0) / cst * 100)
    avg_roi = round(sum(rois) / len(rois), 1) if rois else None
    best = max(pos_rois, key=lambda x: x["roi_pct"]) if pos_rois else None
    worst = min(pos_rois, key=lambda x: x["roi_pct"]) if pos_rois else None

    errors = [{"file": k, "error": v["error"]} for k, v in files.items() if v.get("error")]

    kpis = {
        "equity_pnl": {
            "cash": round(cash, 2), "open_value": round(open_value, 2), "equity": equity,
            "realized_pnl": total_realized,
            "unrealized_pnl": round(unrealized, 2) if marks_live else None,
            "total_pnl": total_pnl, "total_pnl_pct": total_pnl_pct,
            "daily_pnl": daily_pnl, "daily_pnl_pct": daily_pnl_pct,
            "marks_live": marks_live,
        },
        "exposure": {
            "open_cost_basis": open_cost, "open_count": len(open_pos),
            "open_contracts": open_contracts, "deployed_pct": deployed_pct,
        },
        "performance": {
            "settled_count": len(finished), "win_count": wins, "win_rate": win_rate,
            "avg_roi_pct": avg_roi, "best": best, "worst": worst,
            "marks_covered": covered, "open_count": len(open_pos),
        },
        "health": {
            "alive": alive, "position_monitor_age_seconds": pm_age,
            "scan_age_seconds": scan_meta.get("age_seconds"),
            "scan_stale": scan_stale,
            "mode": "PAPER" if PAPER_MODE else "LIVE",
            "errors": errors,
            "prices_enabled": PRICES_ENABLED,
            "prices_ok": (_prices_meta.get("last_poll_ok") if PRICES_ENABLED else None),
        },
    }

    return {
        "server_time": _now().isoformat(),
        "mode": "PAPER" if PAPER_MODE else "LIVE",
        "liveness": {
            "alive": alive,
            "position_monitor_age_seconds": pm_age,
            "interval_seconds": LIVENESS_INTERVAL_S,
        },
        "balance": balance,
        "positions": {
            "open": open_pos,
            "all": positions,
            "summary": {
                "open_count": len(open_pos),
                "total_count": len(positions),
                "total_realized_pnl": total_realized,
                "open_contracts": open_contracts,
                "open_cost_basis": open_cost,
            },
        },
        "orders": {"recent": recent_orders, "counts": order_counts, "total": len(orders)},
        "heartbeats": heartbeats,
        "peaks": peaks or {},
        "calibration": calibration or {},
        "scan": scan,
        "scan_meta": scan_meta,
        "kpis": kpis,
        "files": files,
    }


async def handle_state(_request: web.Request) -> web.Response:
    return web.json_response(build_state(), headers={"Cache-Control": "no-store"})


async def handle_scan(_request: web.Request) -> web.Response:
    scan, err = _read_json(SCAN_FILE)
    if err:
        return web.json_response({"error": err}, status=404)
    return web.json_response(scan, headers={"Cache-Control": "no-store"})


async def handle_index(_request: web.Request) -> web.Response:
    if not DASHBOARD_HTML.exists():
        return web.Response(text="dashboard.html not found", status=404)
    return web.FileResponse(DASHBOARD_HTML, headers={"Cache-Control": "no-store"})


async def handle_prices(_request: web.Request) -> web.Response:
    contracts = []
    for t, r in _prices.items():
        pts = list(r["points"])
        if not pts:
            continue
        contracts.append({
            "ticker": t, "city": r.get("city"), "date": r.get("date"), "label": r.get("label"),
            "bid": r.get("bid"), "ask": r.get("ask"), "last": r.get("last"),
            "mid": r.get("mid"), "volume": r.get("volume"), "held": r.get("held", False),
            "updated": r.get("updated"), "points": pts[-240:],
        })
    contracts.sort(key=lambda c: (c["city"] or "", -(c["volume"] or 0)))
    return web.json_response({**_prices_meta, "contracts": contracts}, headers={"Cache-Control": "no-store"})


def _today_ticker_code(city: str):
    """Today's Kalshi ticker date segment (e.g. '26JUN17') in the city's timezone."""
    from config import STATIONS
    from zoneinfo import ZoneInfo
    cfg = STATIONS.get(city)
    if not cfg:
        return None
    return datetime.now(ZoneInfo(cfg.timezone)).strftime("%y%b%d").upper()


async def handle_temps(_request: web.Request) -> web.Response:
    # Bracket threshold lines, per city, derived from the tracked contracts
    # (held first, then by volume) so the temp curve can be read against them.
    brackets_by_city: dict = {}
    for t, r in _prices.items():
        lo, hi = _bracket_bounds(r.get("label"), t)
        if lo is None and hi is None:
            continue
        brackets_by_city.setdefault(r.get("city"), []).append({
            "ticker": t, "label": r.get("label") or t, "lo": lo, "hi": hi,
            "held": bool(r.get("held")), "volume": r.get("volume") or 0,
        })

    peaks, _ = _read_json(PEAKS_FILE)
    peaks = peaks or {}
    cities = {}
    for city, rec in _temps.items():
        pk = peaks.get(city) or {}
        # The temp series is today-only, so overlay only TODAY's contracts —
        # next-day brackets must not be drawn over (or stretch) today's curve.
        code = _today_ticker_code(city)
        same_day = [b for b in brackets_by_city.get(city, [])
                    if not code or f"-{code}-" in (b["ticker"] or "")]
        bks = sorted(same_day, key=lambda b: (not b["held"], -(b["volume"] or 0)))
        cities[city] = {
            "series": rec.get("series") or [],
            "current": rec.get("current"),
            "high": rec.get("high"),
            "running_max": pk.get("running_max"),
            "peak_confirmed": pk.get("peak_confirmed"),
            "peak_bracket": pk.get("peak_bracket"),
            "max_time": pk.get("max_time"),
            "brackets": bks[:12],
            "updated": rec.get("updated"),
        }
    return web.json_response({**_temps_meta, "cities": cities}, headers={"Cache-Control": "no-store"})


async def handle_health(_request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "mode": "PAPER" if PAPER_MODE else "LIVE"})


def make_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/state", handle_state)
    app.router.add_get("/api/scan", handle_scan)
    app.router.add_get("/api/prices", handle_prices)
    app.router.add_get("/api/temps", handle_temps)
    app.router.add_get("/healthz", handle_health)
    if PRICES_ENABLED or TEMPS_ENABLED:
        app.on_startup.append(_start_poller)
        app.on_cleanup.append(_stop_poller)
    return app


def main() -> None:
    host = os.getenv("DASHBOARD_HOST", "127.0.0.1")
    port = int(os.getenv("DASHBOARD_PORT", "8787"))
    print(f"Weather Edge dashboard → http://{host}:{port}  (mode: {'PAPER' if PAPER_MODE else 'LIVE'})")
    if PRICES_ENABLED:
        print(f"Live price polling: ON — reading Kalshi market data every {PRICE_POLL_SECONDS}s (read-only)")
    web.run_app(make_app(), host=host, port=port, print=None)


if __name__ == "__main__":
    main()
