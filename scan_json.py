#!/usr/bin/env python3
"""JSON wrapper for edge_scanner_v2.py — exports scan data as structured JSON."""

import asyncio
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp
import logging

logger = logging.getLogger(__name__)

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from edge_scanner_v2 import (
    CITIES,
    fetch_ensemble_v2,
    fetch_nws,
    fetch_kalshi_brackets,
    analyze_opportunities_v2,
    get_entry_timing,
)


async def scan_to_json(city_filter: str = None) -> dict:
    """Run full scan and return structured JSON-serializable dict."""
    tz = ZoneInfo("America/New_York")
    now = datetime.now(tz)
    tomorrow = (now + timedelta(days=1)).date()
    target_date_str = tomorrow.isoformat()

    cities_to_scan = (
        {city_filter.upper(): CITIES[city_filter.upper()]}
        if city_filter
        else CITIES
    )

    # Balance
    balance = 0.0
    try:
        from kalshi_client import fetch_balance_quick
        balance = await fetch_balance_quick()
    except Exception as e:
        logger.warning("Failed to fetch balance for scan JSON: %s", e)

    # Existing exposure
    existing_exposure = {}
    try:
        from position_store import load_positions
        import re
        open_pos = [p for p in load_positions() if p.get("status") in ("open", "resting", "pending_sell")]
        series_to_city = {cfg["series"]: code for code, cfg in CITIES.items()}
        for p in open_pos:
            ticker = p.get("ticker", "")
            match = re.match(r'^([A-Z]+)', ticker)
            if match:
                city_code = series_to_city.get(match.group(1))
                if city_code:
                    cost = p.get("contracts", 0) * p.get("avg_price", 0) / 100
                    existing_exposure[city_code] = existing_exposure.get(city_code, 0) + cost
    except Exception as e:
        logger.warning("Failed to load existing exposure for scan JSON: %s", e)

    result = {
        "scan_time": now.isoformat(),
        "target_date": target_date_str,
        "target_display": tomorrow.strftime("%A %B %d, %Y"),
        "balance": balance,
        "cities": [],
        "all_opportunities": [],
        "summary": {},
    }

    async with aiohttp.ClientSession() as session:
        for city_key in cities_to_scan:
            city_data = {
                "key": city_key,
                "name": CITIES[city_key]["name"],
                "status": "ok",
                "error": None,
                "ensemble": None,
                "nws": None,
                "brackets": [],
                "opportunities": [],
                "confidence": {"label": "LOW", "score": 0, "factors": []},
                "entry_window": "",
                "bot_risk": "",
            }

            try:
                ens_task = fetch_ensemble_v2(session, city_key, target_date_str)
                nws_task = fetch_nws(session, city_key, tomorrow)
                mkt_task = fetch_kalshi_brackets(session, city_key)

                results = await asyncio.gather(ens_task, nws_task, mkt_task, return_exceptions=True)

                for i, r in enumerate(results):
                    if isinstance(r, BaseException):
                        raise RuntimeError(f"Fetch error: {r}")

                ensemble, nws_data, brackets = results

                if ensemble.total_count == 0 or not brackets:
                    city_data["status"] = "failed"
                    city_data["error"] = "Missing ensemble or brackets"
                    result["cities"].append(city_data)
                    continue

                # Ensemble summary
                model_data = []
                for mg in ensemble.models:
                    model_data.append({
                        "name": mg.name,
                        "members": len(mg.members),
                        "mean": round(mg.mean, 1),
                        "std": round(mg.std, 1),
                        "weight": mg.weight,
                        "eff_weight": int(len(mg.members) * mg.weight),
                        "is_ai": "aifs" in mg.name.lower(),
                    })

                city_data["ensemble"] = {
                    "total_count": ensemble.total_count,
                    "mean": round(ensemble.mean, 1),
                    "median": round(ensemble.median, 1),
                    "std": round(ensemble.std, 1),
                    "min": round(ensemble.min_val, 1),
                    "max": round(ensemble.max_val, 1),
                    "p10": round(ensemble.p10, 1),
                    "p25": round(ensemble.p25, 1),
                    "p50": round(ensemble.p50, 1),
                    "p75": round(ensemble.p75, 1),
                    "p90": round(ensemble.p90, 1),
                    "kde_bandwidth": round(ensemble.kde_bandwidth, 2),
                    "is_bimodal": ensemble.is_bimodal,
                    "models": model_data,
                }

                # NWS
                city_data["nws"] = {
                    "forecast_high": round(nws_data.forecast_high, 1),
                    "physics_high": round(nws_data.physics_high, 1),
                    "current_temp": round(nws_data.current_temp, 1),
                    "current_wind": round(nws_data.current_wind, 1),
                    "wind_penalty": round(nws_data.wind_penalty, 1),
                    "wet_bulb_penalty": round(nws_data.wet_bulb_penalty, 1),
                    "peak_wind_gust": round(nws_data.peak_wind_gust, 1),
                    "peak_precip_prob": nws_data.peak_precip_prob,
                    "temp_trend": nws_data.temp_trend or "unknown",
                    "is_midnight_high": nws_data.is_midnight_high,
                    "midnight_temp": round(nws_data.midnight_temp, 1),
                    "afternoon_temp": round(nws_data.afternoon_temp, 1),
                }

                # Brackets (raw market data)
                for b in brackets:
                    city_data["brackets"].append({
                        "ticker": b.get("ticker", ""),
                        "title": b.get("title", "") or b.get("subtitle", ""),
                        "yes_bid": b.get("yes_bid", 0),
                        "yes_ask": b.get("yes_ask", 0),
                        "volume": b.get("volume", 0),
                    })

                # Opportunities
                opps = analyze_opportunities_v2(city_key, ensemble, nws_data, brackets, balance, existing_exposure)
                for opp in opps:
                    opp_dict = {
                        "city": opp.city,
                        "bracket_title": opp.bracket_title,
                        "ticker": opp.ticker,
                        "low": opp.low,
                        "high": opp.high,
                        "side": opp.side,
                        "yes_bid": opp.yes_bid,
                        "yes_ask": opp.yes_ask,
                        "volume": opp.volume,
                        "kde_prob": round(opp.kde_prob * 100, 1),
                        "histogram_prob": round(opp.histogram_prob * 100, 1),
                        "edge_raw": round(opp.edge_raw, 1),
                        "edge_after_fees": round(opp.edge_after_fees, 1),
                        "kelly": round(opp.kelly * 100, 1),
                        "suggested_contracts": opp.suggested_contracts,
                        "confidence": opp.confidence,
                        "confidence_score": opp.confidence_score,
                        "trade_score": round(opp.trade_score, 3),
                        "trade_score_components": opp.trade_score_components,
                        "strategies": opp.strategies,
                        "rationale": opp.rationale,
                        "entry_window": opp.entry_window,
                        "bot_risk": opp.bot_risk,
                    }
                    city_data["opportunities"].append(opp_dict)
                    result["all_opportunities"].append(opp_dict)

                # Confidence (city-level)
                if opps:
                    best = max(opps, key=lambda o: o.confidence_score)
                    city_data["confidence"]["label"] = best.confidence
                    city_data["confidence"]["score"] = best.confidence_score

                # Entry window
                window, risk = get_entry_timing(city_key)
                city_data["entry_window"] = window
                city_data["bot_risk"] = risk

            except Exception as e:
                city_data["status"] = "failed"
                city_data["error"] = str(e)

            result["cities"].append(city_data)

    # Summary
    all_opps = result["all_opportunities"]
    tradeable = [o for o in all_opps if o["trade_score"] >= 0.55]
    result["summary"] = {
        "total_opportunities": len(all_opps),
        "tradeable_count": len(tradeable),
        "best_trade_score": max((o["trade_score"] for o in all_opps), default=0),
        "best_confidence": max((o["confidence_score"] for o in all_opps), default=0),
        "cities_scanned": len([c for c in result["cities"] if c["status"] == "ok"]),
        "cities_failed": len([c for c in result["cities"] if c["status"] == "failed"]),
    }

    return result


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Edge Scanner JSON Export")
    parser.add_argument("--city", type=str, default=None)
    parser.add_argument("--output", type=str, default=None, help="Output file (default: stdout)")
    args = parser.parse_args()

    data = asyncio.run(scan_to_json(args.city))

    output = json.dumps(data, indent=2)
    if args.output:
        Path(args.output).write_text(output)
        print(f"Wrote {len(output)} bytes to {args.output}", file=sys.stderr)
    else:
        print(output)
