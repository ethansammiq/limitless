#!/usr/bin/env python3
"""
AUTO SCAN — Automated Edge Scanner with Discord Alerts

Runs edge_scanner_v2 at scheduled intervals, captures output,
and sends Discord webhook alerts when:
  1. A tradeable opportunity is found (TradeScore gate when enabled, else confidence ≥ 90)
  2. An existing position needs attention (morning check)
  3. Market conditions change significantly between scans

Designed to run via cron at optimal windows:
  - 6:00 AM ET  → Morning pre-settlement check
  - 10:00 AM ET → Market open (stale pricing = edge)
  - 3:00 PM ET  → Post-HRRR convergence (OPTIMAL window)
  - 10:00 PM ET → Overnight positioning (next-day setup)

Usage:
  python3 auto_scan.py                # Full scan + Discord alert
  python3 auto_scan.py --city NYC     # Single city
  python3 auto_scan.py --quiet        # Only alert if tradeable
  python3 auto_scan.py --dry-run      # Print what would be sent, don't send

Cron setup (add to crontab -e):
  0 6 * * *   cd /Users/miqadmin/Documents/limitless && /usr/bin/python3 auto_scan.py >> /tmp/auto_scan.log 2>&1
  0 10 * * *  cd /Users/miqadmin/Documents/limitless && /usr/bin/python3 auto_scan.py >> /tmp/auto_scan.log 2>&1
  0 15 * * *  cd /Users/miqadmin/Documents/limitless && /usr/bin/python3 auto_scan.py >> /tmp/auto_scan.log 2>&1
  0 22 * * *  cd /Users/miqadmin/Documents/limitless && /usr/bin/python3 auto_scan.py >> /tmp/auto_scan.log 2>&1
"""

import argparse
import asyncio
import io
import os
import re
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import aiohttp
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

# Import the v2 scanner
from edge_scanner_v2 import (
    CITIES,
    HRRRNBMData,
    MIN_CONFIDENCE_TO_TRADE,
    Opportunity,
    analyze_opportunities_v2,
    fetch_ensemble_v2,
    fetch_hrrr_nbm,
    fetch_kalshi_brackets,
    fetch_nws,
    fetch_orderbook_depth,
    compute_confidence_score,
    is_tomorrow_ticker,
    shorten_bracket_title,
)
from config import (
    TRADE_SCORE_ENABLED, TRADE_SCORE_THRESHOLD, SETTLEMENT_HOUR_ET, STALE_PRICE_ENABLED,
    WATCHLIST, WATCHLIST_MIN_CONFIDENCE,
)
from dutch_book import check_dutch_book, format_dutch_book_alerts
from notifications import send_discord_embeds
from trade_score import compute_trade_score, should_trade
from stale_price_detector import (
    load_previous_state,
    save_current_state,
    build_snapshot,
    detect_stale_prices,
    format_stale_alerts,
)
from log_setup import get_logger
from trade_events import log_event, TradeEvent

logger = get_logger(__name__)

ET = ZoneInfo("America/New_York")
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
SCAN_LOG_DIR = os.path.join(PROJECT_ROOT, "scan_logs")


# Ticker date segment, e.g. KXHIGHNY-26JUN13-B34.5 -> 26JUN13
_TICKER_DATE_RE = re.compile(r"-(\d{2}[A-Z]{3}\d{2})-")


def _is_tradeable(opp) -> bool:
    """Unified tradeable check reading the per-opportunity cached trade score.

    The score is computed exactly once per opportunity in run_scan (see
    _score_opportunity) with the market's own settlement clock; every gate,
    embed, and log reads that cached verdict instead of recomputing with a
    divergent hours-to-settlement basis.
    """
    if TRADE_SCORE_ENABLED:
        components = getattr(opp, "trade_score_components", None) or {}
        if "tradeable" in components:
            return bool(components["tradeable"])
        # Cache miss (score computation failed) — degrade to a fresh check.
        return should_trade(opp, _hours_to_settlement())
    return opp.confidence_score >= MIN_CONFIDENCE_TO_TRADE


def _market_hours_to_settlement(mkt: dict, now: datetime) -> float | None:
    """Hours to settlement for one market, derived from its own payload.

    Prefers the close/expiration timestamp Kalshi returns (same precedence
    as auto_trader); falls back to the ticker's embedded market date — a
    daily-high market for date D settles D+1 at SETTLEMENT_HOUR_ET.
    Returns None when neither source is usable.
    """
    raw = (
        mkt.get("close_time")
        or mkt.get("expected_expiration_time")
        or mkt.get("expiration_time")
    )
    if raw:
        try:
            close_dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if close_dt.tzinfo is None:
                close_dt = close_dt.replace(tzinfo=timezone.utc)  # Kalshi timestamps are UTC
            return max(0.5, (close_dt - now).total_seconds() / 3600)
        except ValueError:
            pass
    match = _TICKER_DATE_RE.search(mkt.get("ticker", ""))
    if not match:
        return None
    try:
        market_date = datetime.strptime(match.group(1), "%y%b%d").date()
    except ValueError:
        return None
    settle_dt = datetime.combine(
        market_date + timedelta(days=1), datetime.min.time()
    ).replace(hour=SETTLEMENT_HOUR_ET, tzinfo=ET)
    return max(0.5, (settle_dt - now).total_seconds() / 3600)


def _hours_to_settlement(market_date=None) -> float:
    """Heuristic fallback when no market payload is available.

    market_date is the day whose high is being traded; it settles the NEXT
    morning at SETTLEMENT_HOUR_ET. Without a date, targets the next
    settlement boundary. Per-opportunity clocks should come from
    _market_hours_to_settlement instead.
    """
    now = datetime.now(ET)
    if market_date is not None:
        settlement_dt = datetime.combine(
            market_date + timedelta(days=1), datetime.min.time()
        ).replace(hour=SETTLEMENT_HOUR_ET, tzinfo=ET)
    else:
        settlement_dt = datetime.combine(now.date(), datetime.min.time()).replace(
            hour=SETTLEMENT_HOUR_ET, tzinfo=ET,
        )
        if now >= settlement_dt:
            settlement_dt += timedelta(days=1)
    return max(0.5, (settlement_dt - now).total_seconds() / 3600)


def _score_opportunity(opp, hours_to_settlement: float, depth=None):
    """Compute the hybrid trade score ONCE and cache it on the opportunity.

    Overwrites the score analyze_opportunities_v2 attached (computed with
    its own lead-time heuristic) so a single market-derived settlement
    clock backs every downstream gate, embed, and calibration record.
    Returns the TradeScore, or None when computation fails.
    """
    try:
        ts = compute_trade_score(opp, hours_to_settlement, depth=depth)
    except Exception as e:
        logger.warning("trade_score computation failed for %s: %s", opp.ticker, e)
        return None
    opp.trade_score = round(ts.score, 4)
    opp.trade_score_components = {
        "confidence_signal": round(ts.confidence_signal, 4),
        "edge_signal": round(ts.edge_signal, 4),
        "urgency_signal": round(ts.urgency_signal, 4),
        "liquidity_penalty": round(ts.liquidity_penalty, 4),
        "w_confidence": round(ts.w_confidence, 4),
        "w_edge": round(ts.w_edge, 4),
        "w_urgency": round(ts.w_urgency, 4),
        "hours_to_settlement": round(hours_to_settlement, 1),
        "tradeable": ts.tradeable,
    }
    return ts


def format_discord_alert(
    all_opps: list[Opportunity],
    city_summaries: list[dict],
    balance: float,
    scan_time: str,
    failed_cities: list[dict] = None,
    hours_to_settlement: float = 14.0,
) -> list[dict]:
    """Format scan results into Discord embed messages."""
    embeds = []
    failed_cities = failed_cities or []

    h2s = hours_to_settlement
    tradeable = [o for o in all_opps if _is_tradeable(o)]
    # Rank by trade score (confidence when disabled) so the best setup leads
    # the alert instead of whichever city happened to scan first.
    if TRADE_SCORE_ENABLED:
        tradeable.sort(key=lambda o: o.trade_score, reverse=True)
    else:
        tradeable.sort(key=lambda o: o.confidence_score, reverse=True)

    # balance <= 0 means the balance fetch failed (or the account is empty):
    # suggested_contracts are all 0, so execute commands would be misleading.
    sizing_ok = balance > 0

    # Header embed
    color = 0x00FF00 if tradeable else 0xFFAA00 if all_opps else 0xFF0000
    status = f"🎯 {len(tradeable)} TRADEABLE" if tradeable else f"👀 {len(all_opps)} opportunities (observe)"

    gate_label = (
        f"TradeScore≥{TRADE_SCORE_THRESHOLD} ({h2s:.0f}h to settle)"
        if TRADE_SCORE_ENABLED
        else f"{MIN_CONFIDENCE_TO_TRADE}+ confidence required"
    )
    header_desc = (
        f"**{status}**\n"
        f"Balance: ${balance:.2f} | Cities: {len(city_summaries)}\n"
        f"Gate: {gate_label}"
    )

    if failed_cities:
        failed_names = [f["city"] for f in failed_cities]
        header_desc += f"\n⚠️ **{len(failed_cities)} city scan(s) failed:** {', '.join(failed_names)}"

    header = {
        "title": f"⚡ EDGE SCANNER v2.0 — {scan_time}",
        "description": header_desc,
        "color": color,
    }
    embeds.append(header)

    # Sizing failure — loud, distinct, and ahead of everything else
    if not sizing_ok:
        embeds.append({
            "title": "🔴 BALANCE UNAVAILABLE — SIZING DISABLED",
            "description": (
                "Account balance could not be fetched this cycle, so position "
                "sizing is disabled and sizing-dependent trading is SKIPPED.\n"
                f"{len(tradeable)} tradeable setup(s) detected but NOT sized — "
                "fix the balance fetch (API credentials / connectivity) to resume."
            ),
            "color": 0xFF0000,
        })

    # Dutch-book arbitrage — riskless, highest priority alert type
    dutch_arbs = [arb for cs in city_summaries for arb in cs.get("dutch_book", [])]
    if dutch_arbs:
        embeds.append({
            "title": "💰 DUTCH BOOK — RISKLESS ARBITRAGE",
            "description": format_dutch_book_alerts(dutch_arbs)[:4096],
            "color": 0xFFD700,
        })

    # Per-city summary
    for cs in city_summaries:
        city_text = (
            f"Ensemble: {cs['mean']:.1f}°F ±{cs['std']:.1f}° ({cs['members']} members)\n"
            f"NWS: {cs['nws_high']:.0f}°F | Physics: {cs['physics']:.1f}°F\n"
            f"Confidence: {cs['conf_label']} ({cs['conf_score']:.0f}/100)\n"
        )
        if cs.get("opps"):
            city_text += f"Opportunities: {len(cs['opps'])}\n"
            for opp in cs["opps"][:3]:  # Top 3 per city
                price = opp.yes_bid if opp.side == "yes" else (100 - opp.yes_ask)
                short = shorten_bracket_title(opp.bracket_title)
                icon = "🎯" if _is_tradeable(opp) else "👀"
                ts_txt = f" TS:{opp.trade_score:.3f}" if TRADE_SCORE_ENABLED and hasattr(opp, "trade_score") and opp.trade_score else ""
                city_text += (
                    f"{icon} {opp.side.upper()} {short} @ {price}¢ "
                    f"(KDE:{opp.kde_prob*100:.0f}% edge:{opp.edge_after_fees*100:+.0f}¢ "
                    f"conf:{opp.confidence_score:.0f}{ts_txt})\n"
                )
        else:
            city_text += "No opportunities above threshold.\n"

        embeds.append({
            "title": f"📍 {cs['name']}",
            "description": city_text,
            "color": 0x00FF00 if any(_is_tradeable(o) for o in cs.get("opps", [])) else 0x808080,
        })

    # Watchlist highlight — flag developing setups before they cross the gate
    for cs in city_summaries:
        city_key = cs["key"]
        if city_key not in WATCHLIST:
            continue
        city_opps = cs.get("opps", [])
        if not city_opps:
            continue
        best_conf = max(o.confidence_score for o in city_opps)
        if best_conf < WATCHLIST_MIN_CONFIDENCE:
            continue
        best = max(city_opps, key=lambda o: o.confidence_score)
        short = shorten_bracket_title(best.bracket_title)
        price = best.yes_bid if best.side == "yes" else (100 - best.yes_ask)
        ts_txt = f" | TS:{best.trade_score:.3f}" if TRADE_SCORE_ENABLED and hasattr(best, "trade_score") and best.trade_score else ""
        watch_text = (
            f"**Best: {best.side.upper()} {short} @ {price}¢**\n"
            f"KDE: {best.kde_prob*100:.1f}% | Edge: {best.edge_after_fees*100:+.1f}¢\n"
            f"Confidence: {best.confidence} ({best.confidence_score:.0f}/100){ts_txt}\n"
            f"Ensemble: {cs['mean']:.1f}°F ±{cs['std']:.1f}° | NWS: {cs['nws_high']:.0f}°F\n"
            f"Trend: {cs.get('temp_trend', '?')}\n"
            f"Watching for conf→90+ at next scan window."
        )
        embeds.append({
            "title": f"👁️ WATCHLIST — {cs['name']}",
            "description": watch_text,
            "color": 0x3498DB,  # Blue — developing, not yet tradeable
        })
        logger.info("WATCHLIST %s: conf=%d, best=%s %s @ %d¢, KDE=%.1f%%",
                     city_key, best_conf, best.side, short, price, best.kde_prob * 100)

    # Tradeable alert (if any) — suppressed when sizing is disabled: the
    # execute commands would all read 0 contracts (red embed above explains).
    if tradeable and sizing_ok:
        alert_text = "**🚨 ACTION REQUIRED — TRADEABLE SETUPS:**\n\n"
        for opp in tradeable:
            # Use bid+1 for maker pricing (0% fee)
            entry_price = opp.yes_bid + 1 if opp.side == "yes" else (100 - opp.yes_ask + 1)
            entry_price = min(entry_price, 50)  # Enforce max entry
            display_price = opp.yes_bid if opp.side == "yes" else (100 - opp.yes_ask)
            short = shorten_bracket_title(opp.bracket_title)
            cost = entry_price / 100 * opp.suggested_contracts
            ts_line = ""
            if TRADE_SCORE_ENABLED and hasattr(opp, "trade_score") and opp.trade_score:
                ts_line = f"TradeScore: {opp.trade_score:.3f} (threshold: {TRADE_SCORE_THRESHOLD})\n"
            alert_text += (
                f"**{opp.city} — {opp.side.upper()} {short} @ {display_price}¢**\n"
                f"KDE: {opp.kde_prob*100:.1f}% | Edge: {opp.edge_after_fees*100:+.1f}¢ | "
                f"Kelly: {opp.suggested_contracts} contracts\n"
                f"Confidence: {opp.confidence} ({opp.confidence_score:.0f}/100)\n"
                f"{ts_line}"
                f"Rationale: {opp.rationale}\n"
                f"**Execute:** `python3 execute_trade.py {opp.ticker} {opp.side} {entry_price} {opp.suggested_contracts}`\n"
                f"Cost: ${cost:.2f} | Payout: ${opp.suggested_contracts:.2f}\n\n"
            )
        alert_text += "⚠️ Copy the command above and run in terminal to execute."
        embeds.append({
            "title": "🎯 TRADEABLE OPPORTUNITIES",
            "description": alert_text[:4096],
            "color": 0x00FF00,
        })

    return embeds


async def run_scan(city_filter: str = None, quiet: bool = False, dry_run: bool = False):
    """Run the full scan and send alerts."""
    now = datetime.now(ET)
    scan_time = now.strftime("%I:%M %p ET, %a %b %d")
    logger.info("AUTO SCAN — %s", scan_time)
    log_event(TradeEvent.SCAN_STARTED, "auto_scan", {"scan_time": scan_time, "city_filter": city_filter})

    # Get balance — sizing derives from it, so a failed fetch must fail LOUD:
    # silently carrying balance=0 turns every opportunity into a misleading
    # 0-contract "budget exhausted" skip and wastes the whole trading window.
    balance = 0.0
    balance_ok = False
    balance_error = ""
    try:
        from kalshi_client import fetch_balance_quick
        for attempt in (1, 2):
            try:
                balance = await fetch_balance_quick()
            except Exception as e:
                balance = 0.0
                balance_error = str(e)
            if balance > 0:
                balance_ok = True
                break
            balance_error = balance_error or "returned $0.00 (auth/API failure or empty account)"
            logger.warning("Balance fetch attempt %d/2 failed: %s", attempt, balance_error)
    except Exception as e:
        balance_error = str(e)
        logger.warning("Balance fetch unavailable: %s", e)
    if not balance_ok:
        logger.error(
            "Balance unavailable after retry — sizing disabled this cycle (%s)",
            balance_error,
        )
        log_event(TradeEvent.ERROR, "auto_scan", {
            "error": "balance_fetch_failed", "detail": balance_error,
        })

    if city_filter:
        city_key_upper = city_filter.upper()
        if city_key_upper not in CITIES:
            valid = ", ".join(sorted(CITIES))
            logger.error("Unknown city '%s'. Valid: %s", city_filter, valid)
            return {"scan_time": scan_time, "balance": balance, "balance_ok": balance_ok,
                    "total_opps": 0, "tradeable": 0, "opps": [], "city_summaries": [],
                    "dutch_book_arbs": [], "log_file": ""}
        cities_to_scan = {city_key_upper: CITIES[city_key_upper]}
    else:
        cities_to_scan = CITIES

    tomorrow = (now + timedelta(days=1)).date()
    target_date_str = tomorrow.isoformat()
    # Fallback settlement clock for the scanned target date; per-opportunity
    # clocks are derived from each market's own payload in the loop below.
    h2s = _hours_to_settlement(tomorrow)

    all_opps = []
    city_summaries = []
    failed_cities = []
    dutch_book_arbs = []

    async with aiohttp.ClientSession() as session:
        for city_key in cities_to_scan:
            logger.info("Scanning %s...", city_key)

            try:
                ens_task = fetch_ensemble_v2(session, city_key, target_date_str)
                nws_task = fetch_nws(session, city_key, tomorrow)
                mkt_task = fetch_kalshi_brackets(session, city_key)
                hrrr_task = fetch_hrrr_nbm(session, city_key, target_date_str)

                results = await asyncio.gather(
                    ens_task, nws_task, mkt_task, hrrr_task, return_exceptions=True,
                )

                # HRRR/NBM is enrichment — degrade to empty rather than fail the city
                if isinstance(results[3], BaseException):
                    logger.debug("HRRR/NBM fetch failed for %s (non-critical): %s",
                                 city_key, results[3])
                    results[3] = HRRRNBMData()

                # Check for exceptions in the critical fetches
                errors = [r for r in results if isinstance(r, Exception)]
                if errors:
                    error_msgs = [f"{type(e).__name__}: {e}" for e in errors]
                    raise RuntimeError(f"Fetch errors: {'; '.join(error_msgs)}")

                ensemble, nws_data, brackets, hrrr_nbm = results

                # ── Dutch-book sweep on the raw ladder (zero extra API calls,
                # before any opportunity filtering so every leg is visible) ──
                try:
                    city_arbs = check_dutch_book(brackets)
                except Exception as db_err:
                    city_arbs = []
                    logger.warning("Dutch-book check failed for %s: %s", city_key, db_err)
                for arb in city_arbs:
                    logger.warning(
                        "DUTCH BOOK %s [%s]: %s-basket, %d legs, +%d¢/set riskless",
                        city_key, arb.event_ticker, arb.side.upper(),
                        len(arb.legs), arb.profit_cents,
                    )
                dutch_book_arbs.extend(city_arbs)

                # ── Order book depth for tomorrow's brackets (non-critical) ──
                city_tz = ZoneInfo(CITIES[city_key]["tz"])
                city_tomorrow = (datetime.now(city_tz) + timedelta(days=1)).date()
                tmrw_tickers = [
                    m.get("ticker", "") for m in brackets
                    if is_tomorrow_ticker(m.get("ticker", ""), city_tomorrow)
                ]
                depth_map = {}
                if tmrw_tickers:
                    try:
                        depth_map = await fetch_orderbook_depth(session, tmrw_tickers)
                    except Exception as depth_err:
                        logger.debug("Orderbook depth fetch failed for %s (non-critical): %s",
                                     city_key, depth_err)

                opps = analyze_opportunities_v2(
                    city_key, ensemble, nws_data, brackets, balance,
                    hrrr_nbm=hrrr_nbm, depth_map=depth_map,
                )

                # ── Single market-derived settlement clock per opportunity ──
                # City-level records use the first tomorrow market's clock.
                city_h2s = h2s
                for m in brackets:
                    if not is_tomorrow_ticker(m.get("ticker", ""), city_tomorrow):
                        continue
                    payload_h2s = _market_hours_to_settlement(m, now)
                    if payload_h2s is not None:
                        city_h2s = payload_h2s
                        break

                mkt_by_ticker = {m.get("ticker", ""): m for m in brackets}
                city_trade_scores = []
                for opp in opps:
                    opp_h2s = _market_hours_to_settlement(
                        mkt_by_ticker.get(opp.ticker, {}), now,
                    ) or city_h2s
                    ts = _score_opportunity(opp, opp_h2s, depth=depth_map.get(opp.ticker))
                    if ts is not None:
                        city_trade_scores.append(ts)
                if TRADE_SCORE_ENABLED:
                    # Re-rank: analyze sorted on its own lead-time heuristic
                    opps.sort(key=lambda o: o.trade_score, reverse=True)
                all_opps.extend(opps)

                conf_label, conf_score, _ = compute_confidence_score(
                    ensemble, nws_data, lead_hours=city_h2s, hrrr_nbm=hrrr_nbm,
                )

                # Save ensemble snapshot for backtest pipeline
                try:
                    from backtest_collector import save_ensemble_snapshot
                    snapshot_data = {
                        "mean": ensemble.mean,
                        "std": ensemble.std,
                        "total_count": ensemble.total_count,
                        "per_model_means": {mg.name: mg.mean for mg in ensemble.models},
                        "nws_forecast_high": nws_data.forecast_high,
                        "physics_high": nws_data.physics_high,
                        "conf_score": conf_score,
                    }
                    save_ensemble_snapshot(city_key, datetime.combine(tomorrow, datetime.min.time()), snapshot_data)
                except Exception as snap_err:
                    logger.warning("Snapshot save failed for %s: %s", city_key, snap_err)

                # Save calibration record (richer snapshot for prediction→outcome loop)
                try:
                    from calibration_tracker import save_calibration_record
                    cal_scan_result = {
                        "mean": ensemble.mean,
                        "std": ensemble.std,
                        "total_count": ensemble.total_count,
                        "kde_bandwidth": getattr(ensemble, "kde_bandwidth", None),
                        "per_model_means": {mg.name: mg.mean for mg in ensemble.models},
                        "per_model_stds": {mg.name: mg.std for mg in ensemble.models},
                        "per_model_counts": {mg.name: len(mg.members) for mg in ensemble.models},
                        "nws_forecast_high": nws_data.forecast_high,
                        "nws_physics_high": nws_data.physics_high,
                        "nws_current_temp": nws_data.current_temp,
                        "nws_wind_penalty": getattr(nws_data, "wind_penalty", 0),
                        "nws_wet_bulb_penalty": getattr(nws_data, "wet_bulb_penalty", 0),
                        "nws_temp_trend": getattr(nws_data, "temp_trend", None),
                    }
                    save_calibration_record(city_key, cal_scan_result, opps, city_trade_scores, city_h2s)
                except Exception as cal_err:
                    logger.warning("Calibration record save failed for %s: %s", city_key, cal_err)

                # Save ALL signals for per-bracket probability calibration
                try:
                    from signal_tracker import save_signals
                    signal_context = {
                        "ensemble_mean": ensemble.mean,
                        "ensemble_std": ensemble.std,
                        "nws_forecast_high": nws_data.forecast_high,
                        "nws_physics_high": nws_data.physics_high,
                        "lead_time_hours": city_h2s,
                    }
                    n_signals = save_signals(city_key, opps, signal_context)
                    if n_signals:
                        logger.info("  %s: saved %d signals", city_key, n_signals)
                except Exception as sig_err:
                    logger.warning("Signal save failed for %s: %s", city_key, sig_err)

                city_summaries.append({
                    "name": CITIES[city_key]["name"],
                    "key": city_key,
                    "mean": ensemble.mean,
                    "std": ensemble.std,
                    "members": ensemble.total_count,
                    "nws_high": nws_data.forecast_high,
                    "physics": nws_data.physics_high,
                    "current_temp": nws_data.current_temp,
                    "temp_trend": nws_data.temp_trend,
                    "conf_label": conf_label,
                    "conf_score": conf_score,
                    "opps": opps,
                    # Raw market dicts — stale-price detection needs brackets
                    # that did NOT survive the opportunity gates.
                    "brackets": brackets,
                    "dutch_book": city_arbs,
                })

                tradeable_count = sum(1 for o in opps if _is_tradeable(o))
                logger.info("  %s: %d members, %d opps (%d tradeable)",
                            city_key, ensemble.total_count, len(opps), tradeable_count)
                log_event(TradeEvent.SCAN_CITY_COMPLETE, "auto_scan", {
                    "city": city_key, "members": ensemble.total_count,
                    "opps": len(opps), "tradeable": tradeable_count,
                    "mean": round(ensemble.mean, 1), "std": round(ensemble.std, 2),
                    "conf_score": round(conf_score, 1),
                })

            except Exception as e:
                failed_cities.append({"city": city_key, "error": str(e)})
                logger.error("  %s: FAILED — %s", city_key, e)
                log_event(TradeEvent.SCAN_CITY_FAILED, "auto_scan", {
                    "city": city_key, "error": str(e),
                })
                continue

    if failed_cities:
        failed_names = [f["city"] for f in failed_cities]
        scanned_count = len(cities_to_scan) - len(failed_cities)
        logger.warning("%d city scan(s) failed: %s (%d/%d succeeded)",
                       len(failed_cities), ", ".join(failed_names),
                       scanned_count, len(cities_to_scan))

    # ── Stale Price Detection ──
    stale_alerts = []
    if STALE_PRICE_ENABLED:
        prev_state = load_previous_state()
        curr_state = {}
        for cs in city_summaries:
            city_key = cs["key"]
            # Feed the FULL raw bracket list: a stale bracket is typically one
            # that was fairly priced last scan (not an opportunity) and failed
            # to reprice — pre-filtered opps can never surface it.
            snapshot = build_snapshot(city_key, cs["mean"], cs["std"], cs.get("brackets", []))
            curr_state[city_key] = snapshot

            # Detect stale prices for this city
            city_alerts = detect_stale_prices(city_key, snapshot, prev_state.get(city_key))
            stale_alerts.extend(city_alerts)

        save_current_state(curr_state)

        if stale_alerts:
            logger.info("STALE PRICES: %d bracket(s) may not have repriced", len(stale_alerts))
            for sa in stale_alerts[:3]:
                logger.info("  %s %s: mean shifted %+.1f°F, bid delta %+d¢",
                            sa.city, sa.ticker, sa.mean_shift_f, sa.actual_bid - sa.prev_bid)

    # Summary
    tradeable = [o for o in all_opps if _is_tradeable(o)]
    logger.info("Total: %d opportunities, %d tradeable | Balance: $%.2f",
                len(all_opps), len(tradeable), balance)

    # Save scan log
    os.makedirs(SCAN_LOG_DIR, exist_ok=True)
    log_file = os.path.join(SCAN_LOG_DIR, f"scan_{now.strftime('%Y%m%d_%H%M')}.txt")

    # Capture full v2 output to log
    buf = io.StringIO()
    with redirect_stdout(buf):
        from edge_scanner_v2 import print_summary_v2
        print_summary_v2(all_opps, balance)
    summary_output = buf.getvalue()

    with open(log_file, "w") as f:
        f.write(f"AUTO SCAN — {scan_time}\n")
        f.write(f"Balance: ${balance:.2f}\n")
        f.write(f"Opportunities: {len(all_opps)} total, {len(tradeable)} tradeable\n\n")
        for cs in city_summaries:
            f.write(f"{cs['name']}: {cs['mean']:.1f}°F ±{cs['std']:.1f}° | "
                    f"NWS: {cs['nws_high']:.0f}°F | Conf: {cs['conf_label']} ({cs['conf_score']:.0f})\n")
            for opp in cs["opps"]:
                price = opp.yes_bid if opp.side == "yes" else (100 - opp.yes_ask)
                short = shorten_bracket_title(opp.bracket_title)
                gate = "★" if _is_tradeable(opp) else " "
                ts_log = f" TS:{opp.trade_score:.3f}" if TRADE_SCORE_ENABLED and hasattr(opp, "trade_score") and opp.trade_score else ""
                f.write(f"  {gate} {opp.side.upper()} {short} @ {price}¢ | "
                        f"KDE:{opp.kde_prob*100:.0f}% edge:{opp.edge_after_fees*100:+.0f}¢ "
                        f"conf:{opp.confidence_score:.0f}{ts_log}\n")
        f.write(f"\n{summary_output}")

    logger.info("Log saved: %s", log_file)

    # Check if any watchlist city has a notable setup
    watchlist_active = any(
        cs["key"] in WATCHLIST
        and any(o.confidence_score >= WATCHLIST_MIN_CONFIDENCE for o in cs.get("opps", []))
        for cs in city_summaries
    )

    # Send Discord alert — Dutch books and balance failures always alert,
    # even in quiet mode (riskless arb / disabled sizing must not be silent)
    if quiet and not tradeable and not stale_alerts and not watchlist_active \
            and not dutch_book_arbs and balance_ok:
        logger.info("Quiet mode — no tradeable opportunities, skipping Discord alert")
    else:
        # Display clock: tightest cached market clock, else the scan fallback
        cached_hours = [
            o.trade_score_components.get("hours_to_settlement")
            for o in all_opps if getattr(o, "trade_score_components", None)
        ]
        cached_hours = [h for h in cached_hours if h]
        display_h2s = min(cached_hours) if cached_hours else h2s
        embeds = format_discord_alert(
            all_opps, city_summaries, balance, scan_time, failed_cities, display_h2s,
        )

        # Add stale price alert embed if any
        if stale_alerts:
            stale_text = format_stale_alerts(stale_alerts)
            if stale_text:
                embeds.append({
                    "title": "📊 STALE PRICE DETECTION",
                    "description": stale_text[:4096],
                    "color": 0xFF6600,
                })

        await send_discord_embeds(embeds, dry_run=dry_run, context="auto_scan")

    log_event(TradeEvent.SCAN_COMPLETE, "auto_scan", {
        "total_opps": len(all_opps), "tradeable": len(tradeable),
        "cities_scanned": len(city_summaries), "cities_failed": len(failed_cities),
        "stale_alerts": len(stale_alerts), "balance": round(balance, 2),
        "balance_ok": balance_ok, "dutch_book": len(dutch_book_arbs),
    })

    # Record successful completion for watchdog
    from heartbeat import write_heartbeat
    write_heartbeat("auto_scan")

    # Return results for programmatic use
    return {
        "scan_time": scan_time,
        "balance": balance,
        # False when the balance fetch failed after retry — sizing-dependent
        # trading (auto_trader) must skip this cycle rather than place
        # 0-contract orders against a phantom $0 balance.
        "balance_ok": balance_ok,
        "total_opps": len(all_opps),
        "tradeable": len(tradeable),
        "opps": all_opps,
        "city_summaries": city_summaries,
        "dutch_book_arbs": dutch_book_arbs,
        "log_file": log_file,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Auto Scan — Automated Edge Scanner with Discord Alerts")
    parser.add_argument("--city", type=str, default=None, help="City code (NYC, CHI, DEN, MIA, LAX)")
    parser.add_argument("--quiet", action="store_true", help="Only send Discord alert if tradeable setup found")
    parser.add_argument("--dry-run", action="store_true", help="Print what would be sent, don't actually send")
    args = parser.parse_args()
    asyncio.run(run_scan(args.city, args.quiet, args.dry_run))
