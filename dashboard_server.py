#!/usr/bin/env python3
"""Live dashboard server for Weather Edge.

Read-only aiohttp server that serves dashboard.html and exposes the bot's
live state files as JSON, so the dashboard can auto-refresh and show the
*actual* system state (service heartbeats, live account snapshot, peak
monitor, market prices, temps, opportunity radar) instead of a one-shot
static load.

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
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from aiohttp import web

ROOT = Path(__file__).resolve().parent

# Load .env BEFORE any config import (mirrors the cron jobs).
try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except Exception:
    pass

HEARTBEATS_FILE = ROOT / "heartbeats.json"
SHADOW_DIR = ROOT / "logs" / "shadow_books"
# Real-money account snapshot, written by live_watch.py each run — the only
# money this dashboard shows (the KDE paper ledger was retired 2026-07-06).
LIVE_ACCOUNT_FILE = ROOT / "logs" / "live_account.json"
PEAKS_FILE = ROOT / "peak_state.json"
DASHBOARD_HTML = ROOT / "dashboard.html"

# cli_sniper runs every 2 min, 24/7 — the true liveness signal for the
# settlement-source system.
LIVENESS_INTERVAL_S = 120

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
# Manual watchlist: tickers always tracked and badged even when unheld and too
# illiquid to make the per-city volume cut. {"tickers": ["KXHIGH..."]}
WATCHLIST_FILE = ROOT / "dashboard_watchlist.json"

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

# city -> {updated, brackets: [{ticker,label,lo,hi,bid,ask,volume}]} — the FULL
# today-ladder for the opportunity radar (unlike _prices, which keeps only the
# top-N by volume: dead tails are exactly the low-volume contracts that drops).
_radar: dict = {}

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
    (matches the repo convention in core.io / peak_monitor / heartbeat)."""
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


def _ingest(city: str, markets: list, now_iso: str, held: set | None = None,
            watched: set | None = None) -> None:
    held = held or set()
    watched = watched or set()
    scored = []
    for m in markets or []:
        t = m.get("ticker")
        if not t:
            continue
        bid, ask, last, vol = _market_quote(m)
        scored.append((vol, t, m, bid, ask, last))
    scored.sort(key=lambda x: x[0], reverse=True)
    # Keep the top-N by volume per city, PLUS any ticker we hold a position in
    # or manually watch, so those contracts are always charted even illiquid.
    pinned = held | watched
    keep = scored[:PRICE_TOP_N] + [s for s in scored[PRICE_TOP_N:] if s[1] in pinned]
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
                   updated=now_iso, held=(t in held), watched=(t in watched))
        rec["points"].append({"t": now_iso, "mid": mid, "bid": bid, "ask": ask})


def _radar_ingest(city: str, markets: list, now_iso: str) -> None:
    """Full today-ladder snapshot from a get_markets response (no extra API
    calls — piggybacks on the price poller's fetch)."""
    code = _today_ticker_code(city)
    if not code:
        return
    rows = []
    for m in markets or []:
        t = m.get("ticker") or ""
        if f"-{code}-" not in t:
            continue
        bid, ask, last, vol = _market_quote(m)
        label = (m.get("yes_sub_title") or m.get("subtitle") or "").strip()
        lo, hi = _bracket_bounds(label, t)
        rows.append({"ticker": t, "label": label, "lo": lo, "hi": hi,
                     "bid": bid, "ask": ask, "volume": vol})
    rows.sort(key=lambda r: r["lo"] if r["lo"] is not None else -999.0)
    _radar[city] = {"updated": now_iso, "brackets": rows}


def _radar_status(lo, hi, certain) -> str:
    """Classify a bracket against the certain CLI settle bound.
      dead   — ceiling below what the station already observed; any bid is free money
      leader — contains the minimum certain settle (raw running max)
      target — contains certain+1, the ~+0.8°F CLI-vs-METAR offset bracket
      open   — still reachable, no observation verdict yet
    """
    if certain is None:
        return "open"
    if hi is not None and hi < certain:
        return "dead"
    if (lo is None or lo <= certain) and (hi is None or certain <= hi):
        return "leader"
    if (lo is None or lo <= certain + 1) and (hi is None or certain + 1 <= hi):
        return "target"
    return "open"


def _held_tickers() -> set:
    """Tickers with a nonzero live position (live_watch.py account snapshot)."""
    data, _ = _read_json(LIVE_ACCOUNT_FILE)
    return {
        p.get("ticker")
        for p in (data or {}).get("open_positions", [])
        if p.get("ticker") and p.get("qty")
    }


def _watched_tickers() -> set:
    """Manual watchlist tickers (dashboard_watchlist.json)."""
    data, _ = _read_json(WATCHLIST_FILE)
    return {t for t in (data or {}).get("tickers", []) if t}


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
    watched = _watched_tickers()
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
            _ingest(city, mk, now_iso, held, watched)
            _radar_ingest(city, mk, now_iso)
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


def build_state() -> dict:
    """Service health + peaks. Money lives at /api/live (live_watch snapshot);
    the paper equity/positions/orders/scan KPIs died with the KDE engine."""
    files = {}

    heartbeats_raw, herr = _read_json(HEARTBEATS_FILE)
    files["heartbeats"] = {**_file_meta(HEARTBEATS_FILE), "error": herr}
    heartbeats = {}
    for svc, hb in (heartbeats_raw or {}).items():
        ts = hb.get("timestamp") if isinstance(hb, dict) else None
        heartbeats[svc] = {"timestamp": ts, "age_seconds": _age(ts)}
    sniper_age = heartbeats.get("cli_sniper", {}).get("age_seconds")
    alive = sniper_age is not None and sniper_age < LIVENESS_INTERVAL_S * 3  # <6 min

    peaks, pkerr = _read_json(PEAKS_FILE)
    files["peaks"] = {**_file_meta(PEAKS_FILE), "error": pkerr}

    files["live_account"] = {**_file_meta(LIVE_ACCOUNT_FILE), "error": None}

    errors = [{"file": k, "error": v["error"]} for k, v in files.items() if v.get("error")]

    kpis = {
        "health": {
            "alive": alive, "cli_sniper_age_seconds": sniper_age,
            "mode": "LIVE",
            "errors": errors,
            "prices_enabled": PRICES_ENABLED,
            "prices_ok": (_prices_meta.get("last_poll_ok") if PRICES_ENABLED else None),
        },
    }

    return {
        "server_time": _now().isoformat(),
        "mode": "LIVE",
        "liveness": {
            "alive": alive,
            "cli_sniper_age_seconds": sniper_age,
            "interval_seconds": LIVENESS_INTERVAL_S,
        },
        "heartbeats": heartbeats,
        "peaks": peaks or {},
        "kpis": kpis,
        "files": files,
    }


async def handle_state(_request: web.Request) -> web.Response:
    return web.json_response(build_state(), headers={"Cache-Control": "no-store"})


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
            "watched": r.get("watched", False),
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


async def handle_live(_request: web.Request) -> web.Response:
    """Real-money account snapshot (live_watch.py output). Distinct from
    /api/state, which is service health."""
    data, err = _read_json(LIVE_ACCOUNT_FILE)
    if err or not data:
        return web.json_response(
            {"available": False, "reason": "no live snapshot yet — schedule live_watch.py"},
            headers={"Cache-Control": "no-store"})
    age = None
    if data.get("updated"):
        try:
            age = (datetime.now(timezone.utc)
                   - datetime.fromisoformat(data["updated"])).total_seconds()
        except ValueError:
            age = None
    return web.json_response({"available": True, "age_seconds": age, **data},
                             headers={"Cache-Control": "no-store"})


async def handle_radar(_request: web.Request) -> web.Response:
    """Opportunity radar: today's full ladder per city classified against the
    settlement station's observed running max (the 2026-07-02 dead-bracket
    find, as a live panel instead of a Discord ping)."""
    from core.obs import certain_min_settle, corroborated_extreme

    cities = {}
    for city, ladder in _radar.items():
        rec = _temps.get(city) or {}
        temps = [p.get("temp") for p in rec.get("series") or [] if p.get("temp") is not None]
        extreme = corroborated_extreme(temps, "high")
        certain = certain_min_settle(extreme) if extreme is not None else None
        brackets = []
        for b in ladder["brackets"]:
            status = _radar_status(b["lo"], b["hi"], certain)
            alert = status == "dead" and (b["bid"] or 0) >= 5
            brackets.append({**b, "status": status, "alert": alert})
        cities[city] = {
            "running_max": round(extreme, 1) if extreme is not None else None,
            "certain_settle": certain,
            "obs_count": len(temps),
            "obs_updated": rec.get("updated"),
            "prices_updated": ladder["updated"],
            "brackets": brackets,
            "alert_count": sum(1 for b in brackets if b["alert"]),
        }
    return web.json_response(
        {"enabled": PRICES_ENABLED and TEMPS_ENABLED, "cities": cities},
        headers={"Cache-Control": "no-store"})


# (path, mtime) -> parsed walls, so each 15s dashboard tick doesn't re-parse
# an unchanged shadow journal (it only grows every 30 min).
_walls_cache: dict = {}


def _load_shadow_walls(path: Path) -> dict:
    key = str(path)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    cached = _walls_cache.get(key)
    if cached and cached[0] == mtime:
        return cached[1]
    from core.walls import scan_rows

    rows = []
    for line in path.read_text().splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("venue") == "kalshi" and row.get("live"):
            rows.append(row)
    walls = scan_rows(rows)
    _walls_cache.clear()  # keep at most one file parsed
    _walls_cache[key] = (mtime, walls)
    return walls


async def handle_walls(_request: web.Request) -> web.Response:
    """Certainty-wall watch: informed size resting in today's shadow books
    (competitor dossier 2026-07-07 — walls mark defended settlement theses;
    a wall predating its public data marks a faster-flow station)."""
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    walls = _load_shadow_walls(SHADOW_DIR / f"{day}.jsonl")
    out = []
    for ticker, rec in walls.items():
        for side in ("yes", "no"):
            w = rec.get(f"{side}_wall")
            if not w:
                continue
            out.append({
                "ticker": ticker, "series": rec.get("series"),
                "target_date": rec.get("target_date"), "side": side,
                "total": w["total"], "max_level": w["max_level"],
                "ladder_levels": w["ladder_levels"], "band": w["band"],
                "kind": w["kind"],
                "first_seen": rec.get(f"first_seen_{side}"),
                "last_seen": rec.get("ts"),
                "yes_bid": rec.get("yes_bid"), "yes_ask": rec.get("yes_ask"),
            })
    # Defended theses first (the tradeable context), then mid, farms last.
    rank = {"defense": 0, "mid": 1, "penny_farm": 2}
    out.sort(key=lambda r: (rank.get(r["kind"], 3), -r["total"]))
    return web.json_response(
        {"day": day, "walls": out, "count": len(out)},
        headers={"Cache-Control": "no-store"})


async def handle_health(_request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "mode": "LIVE"})


def make_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/state", handle_state)
    app.router.add_get("/api/prices", handle_prices)
    app.router.add_get("/api/temps", handle_temps)
    app.router.add_get("/api/live", handle_live)
    app.router.add_get("/api/radar", handle_radar)
    app.router.add_get("/api/walls", handle_walls)
    app.router.add_get("/healthz", handle_health)
    if PRICES_ENABLED or TEMPS_ENABLED:
        app.on_startup.append(_start_poller)
        app.on_cleanup.append(_stop_poller)
    return app


def main() -> None:
    host = os.getenv("DASHBOARD_HOST", "127.0.0.1")
    port = int(os.getenv("DASHBOARD_PORT", "8787"))
    print(f"Weather Edge dashboard → http://{host}:{port}")
    if PRICES_ENABLED:
        print(f"Live price polling: ON — reading Kalshi market data every {PRICE_POLL_SECONDS}s (read-only)")
    web.run_app(make_app(), host=host, port=port, print=None)


if __name__ == "__main__":
    main()
