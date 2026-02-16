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
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import aiohttp
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

# Import the v2 scanner
from edge_scanner_v2 import (
    CITIES,
    MIN_CONFIDENCE_TO_TRADE,
    Opportunity,
    analyze_opportunities_v2,
    fetch_ensemble_v2,
    fetch_kalshi_brackets,
    fetch_nws,
    compute_confidence_score,
    shorten_bracket_title,
)
from config import TRADE_SCORE_ENABLED, TRADE_SCORE_THRESHOLD, SETTLEMENT_HOUR_ET, STALE_PRICE_ENABLED
from notifications import send_discord_embeds
from trade_score import should_trade
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


def _is_tradeable(opp, hours_to_settlement: float = 14.0) -> bool:
    """Unified tradeable check: uses trade score when enabled, else legacy 90-gate."""
    if TRADE_SCORE_ENABLED:
        return should_trade(opp, hours_to_settlement)
    return opp.confidence_score >= MIN_CONFIDENCE_TO_TRADE


def _hours_to_settlement() -> float:
    """Compute hours until the next settlement.

    If we're before today's settlement hour, target today.
    If we're past it, target tomorrow.  This prevents the 6 AM scan
    from returning ~25 h (tomorrow) when settlement is actually ~1 h away (today).
    """
    now = datetime.now(ET)
    today_settle = datetime.combine(now.date(), datetime.min.time()).replace(
        hour=SETTLEMENT_HOUR_ET, tzinfo=ET,
    )
    if now < today_settle:
        settlement_dt = today_settle
    else:
        tomorrow = now.date() + timedelta(days=1)
        settlement_dt = datetime.combine(tomorrow, datetime.min.time()).replace(
            hour=SETTLEMENT_HOUR_ET, tzinfo=ET,
        )
    return max(0.5, (settlement_dt - now).total_seconds() / 3600)


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
    tradeable = [o for o in all_opps if _is_tradeable(o, h2s)]

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
                icon = "🎯" if _is_tradeable(opp, h2s) else "👀"
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
            "color": 0x00FF00 if any(_is_tradeable(o, h2s) for o in cs.get("opps", [])) else 0x808080,
        })

    # Tradeable alert (if any)
    if tradeable:
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

    # Get balance
    balance = 0.0
    try:
        from kalshi_client import fetch_balance_quick
        balance = await fetch_balance_quick()
    except Exception as e:
        logger.warning("Balance fetch failed: %s", e)

    if city_filter:
        city_key_upper = city_filter.upper()
        if city_key_upper not in CITIES:
            valid = ", ".join(sorted(CITIES))
            logger.error("Unknown city '%s'. Valid: %s", city_filter, valid)
            return {"scan_time": scan_time, "balance": balance, "total_opps": 0,
                    "tradeable": 0, "opps": [], "city_summaries": [], "log_file": ""}
        cities_to_scan = {city_key_upper: CITIES[city_key_upper]}
    else:
        cities_to_scan = CITIES

    tomorrow = (now + timedelta(days=1)).date()
    target_date_str = tomorrow.isoformat()
    h2s = _hours_to_settlement()

    all_opps = []
    city_summaries = []
    failed_cities = []

    async with aiohttp.ClientSession() as session:
        for city_key in cities_to_scan:
            logger.info("Scanning %s...", city_key)

            try:
                ens_task = fetch_ensemble_v2(session, city_key, target_date_str)
                nws_task = fetch_nws(session, city_key, tomorrow)
                mkt_task = fetch_kalshi_brackets(session, city_key)

                results = await asyncio.gather(ens_task, nws_task, mkt_task, return_exceptions=True)

                # Check for exceptions in any of the three fetches
                errors = [r for r in results if isinstance(r, Exception)]
                if errors:
                    error_msgs = [f"{type(e).__name__}: {e}" for e in errors]
                    raise RuntimeError(f"Fetch errors: {'; '.join(error_msgs)}")

                ensemble, nws_data, brackets = results
                opps = analyze_opportunities_v2(city_key, ensemble, nws_data, brackets, balance)
                all_opps.extend(opps)

                conf_label, conf_score, _ = compute_confidence_score(ensemble, nws_data)

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
                    from trade_score import compute_trade_score
                    city_trade_scores = [compute_trade_score(o, h2s) for o in opps]
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
                    save_calibration_record(city_key, cal_scan_result, opps, city_trade_scores, h2s)
                except Exception as cal_err:
                    logger.warning("Calibration record save failed for %s: %s", city_key, cal_err)

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
                })

                tradeable_count = sum(1 for o in opps if _is_tradeable(o, h2s))
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
            # Rebuild bracket data from the scan (we need raw brackets per city)
            # The city_summaries have opps but not raw brackets, so build from opps
            bracket_bids = {}
            for opp in cs.get("opps", []):
                bracket_bids[opp.ticker] = {
                    "bid": opp.yes_bid,
                    "title": opp.bracket_title,
                }
            snapshot = build_snapshot(city_key, cs["mean"], cs["std"], [])
            # Override bracket_bids from opportunity data (more complete)
            snapshot.bracket_bids = bracket_bids
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
    tradeable = [o for o in all_opps if _is_tradeable(o, h2s)]
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
                gate = "★" if _is_tradeable(opp, h2s) else " "
                ts_log = f" TS:{opp.trade_score:.3f}" if TRADE_SCORE_ENABLED and hasattr(opp, "trade_score") and opp.trade_score else ""
                f.write(f"  {gate} {opp.side.upper()} {short} @ {price}¢ | "
                        f"KDE:{opp.kde_prob*100:.0f}% edge:{opp.edge_after_fees*100:+.0f}¢ "
                        f"conf:{opp.confidence_score:.0f}{ts_log}\n")
        f.write(f"\n{summary_output}")

    logger.info("Log saved: %s", log_file)

    # Send Discord alert
    if quiet and not tradeable and not stale_alerts:
        logger.info("Quiet mode — no tradeable opportunities, skipping Discord alert")
    else:
        embeds = format_discord_alert(all_opps, city_summaries, balance, scan_time, failed_cities, h2s)

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
    })

    # Record successful completion for watchdog
    from heartbeat import write_heartbeat
    write_heartbeat("auto_scan")

    # Return results for programmatic use
    return {
        "scan_time": scan_time,
        "balance": balance,
        "total_opps": len(all_opps),
        "tradeable": len(tradeable),
        "opps": all_opps,
        "city_summaries": city_summaries,
        "log_file": log_file,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Auto Scan — Automated Edge Scanner with Discord Alerts")
    parser.add_argument("--city", type=str, default=None, help="City code (NYC, CHI, DEN, MIA, LAX)")
    parser.add_argument("--quiet", action="store_true", help="Only send Discord alert if tradeable setup found")
    parser.add_argument("--dry-run", action="store_true", help="Print what would be sent, don't actually send")
    args = parser.parse_args()
    asyncio.run(run_scan(args.city, args.quiet, args.dry_run))
