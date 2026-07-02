#!/usr/bin/env python3
"""
BACKTEST SIMULATOR — Exit strategy evaluation via Brownian bridge simulation.

Uses historical bracket settlement data (daily_data.jsonl) to simulate
different exit strategies against known outcomes.  Answers:
  "Do exit upgrades improve P&L vs. baseline?  By how much?"

We have settlement outcomes but no intraday price paths.  We generate
parametric Brownian-bridge price paths anchored to known entry +
settlement, then run the existing pure exit functions against each path.

Usage:
    python3 backtest_simulator.py                           # All cities, all time
    python3 backtest_simulator.py --city LAX --days 365     # LAX last year
    python3 backtest_simulator.py --entry 20 25 30          # Specific entries
    python3 backtest_simulator.py --compare                 # All strategies, comparison table
    python3 backtest_simulator.py --json                    # Machine-readable output
"""

import argparse
import json
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import numpy as np

# ── Reuse existing loaders ──
from backtest_analyzer import load_records

# ── Import pure exit helpers from position_monitor ──
from position_monitor import (
    _trailing_offset_for_price,
    _scaled_trailing_offset,
    _scaled_freeroll_multiplier,
    _scaled_mid_profit_threshold,
    _adaptive_freeroll_multiplier,
    _check_momentum_drop,
)

# ── Import config constants ──
from config import (
    FREEROLL_MULTIPLIER,
    CAPITAL_EFFICIENCY_THRESHOLD_CENTS,
    MID_PROFIT_THRESHOLD_CENTS,
    MID_PROFIT_SELL_FRACTION,
)

PROJECT_ROOT = Path(__file__).resolve().parent

# ═══════════════════════════════════════════════════════════════
#  DATA STRUCTURES
# ═══════════════════════════════════════════════════════════════


@dataclass
class BracketInfo:
    """A single bracket from settlement data."""

    ticker: str
    floor_strike: float
    cap_strike: float
    strike_type: str  # "between", "greater", "less"
    result: str  # "yes" or "no"
    yes_bid_close: int  # Closing price (0-99 cents)
    volume: int


@dataclass
class PriceTick:
    """A single point in a simulated price path."""

    minutes_from_entry: int
    price_cents: int
    hours_to_settlement: float


@dataclass
class ExitEvent:
    """Records when and why an exit was triggered."""

    exit_type: str  # freeroll, efficiency, trailing, mid_profit, momentum, hold_to_settle
    exit_price_cents: int
    minutes_from_entry: int
    contracts_sold: int
    contracts_remaining: int
    pnl_cents: float  # Total P&L for this exit (across contracts_sold)


@dataclass
class SimulatedTrade:
    """Complete record of a single simulated trade."""

    date: str
    city: str
    ticker: str
    bracket_low: float
    bracket_high: float
    side: str
    entry_price_cents: int
    settlement_value: int  # 100 (win) or 0 (loss)
    contracts: int

    exit_events: list = field(default_factory=list)
    total_pnl_cents: float = 0.0
    hold_to_settle_pnl: float = 0.0
    peak_price: int = 0
    strategy_name: str = "baseline"


@dataclass
class StrategyConfig:
    """Toggle-able strategy configuration for A/B comparison."""

    name: str
    time_decay_enabled: bool = False
    adaptive_freeroll_enabled: bool = False
    momentum_enabled: bool = False
    mid_profit_enabled: bool = False
    freeroll_enabled: bool = True
    efficiency_exit_enabled: bool = True
    trailing_stop_enabled: bool = True

    freeroll_multiplier: float = FREEROLL_MULTIPLIER
    efficiency_threshold: int = CAPITAL_EFFICIENCY_THRESHOLD_CENTS
    mid_profit_threshold: int = MID_PROFIT_THRESHOLD_CENTS


@dataclass
class StrategyReport:
    """Aggregated statistics for one strategy configuration."""

    strategy_name: str
    total_trades: int = 0
    winning_trades: int = 0
    win_rate: float = 0.0
    total_pnl_cents: float = 0.0
    avg_pnl_per_trade: float = 0.0
    max_drawdown_cents: float = 0.0
    sharpe_ratio: float = 0.0

    exit_counts: dict = field(default_factory=dict)
    avg_exit_price: dict = field(default_factory=dict)
    pnl_vs_hold: float = 0.0

    pnl_by_entry_price: dict = field(default_factory=dict)
    winrate_by_entry_price: dict = field(default_factory=dict)


# ═══════════════════════════════════════════════════════════════
#  STRATEGY PRESETS
# ═══════════════════════════════════════════════════════════════

STRATEGY_PRESETS: dict[str, StrategyConfig] = {
    "hold_to_settlement": StrategyConfig(
        name="Hold to Settlement",
        freeroll_enabled=False,
        efficiency_exit_enabled=False,
        trailing_stop_enabled=False,
    ),
    "baseline": StrategyConfig(
        name="Baseline (2x freeroll, 8c trail)",
    ),
    "time_decay": StrategyConfig(
        name="+ Time Decay",
        time_decay_enabled=True,
    ),
    "adaptive_freeroll": StrategyConfig(
        name="+ Adaptive Freeroll",
        adaptive_freeroll_enabled=True,
    ),
    "momentum": StrategyConfig(
        name="+ Momentum Exit",
        momentum_enabled=True,
    ),
    "mid_profit": StrategyConfig(
        name="+ Mid-Profit Take",
        mid_profit_enabled=True,
    ),
    "all_upgrades": StrategyConfig(
        name="All Upgrades",
        time_decay_enabled=True,
        adaptive_freeroll_enabled=True,
        momentum_enabled=True,
        mid_profit_enabled=True,
    ),
}


# ═══════════════════════════════════════════════════════════════
#  PRICE PATH GENERATION (Brownian Bridge)
# ═══════════════════════════════════════════════════════════════


def simulate_price_path(
    entry_price_cents: int,
    settlement_value: int,
    total_minutes: int = 720,
    tick_interval_minutes: int = 5,
    seed: int = 42,
) -> list[PriceTick]:
    """Generate a synthetic intraday price path using a Brownian bridge
    with observation-release shock events.

    A Brownian bridge is a random walk conditioned on starting at entry_price
    and ending near settlement_value.  Mathematically:
        B(t) = (1-t)·X₀ + t·Xₜ + σ·√(t(1-t))·Z

    Observation Shocks
    ------------------
    Real weather markets have sudden 10-30¢ jumps when DSM, 6-hour obs,
    or HRRR model runs are released.  We inject 3-5 discrete shock events
    at realistic intervals (roughly every 2-3 hours).  During these ticks,
    normal smoothing is bypassed.

    - Winners: shocks bias upward (+5 to +25¢), with ~20% negative shocks
    - Losers:  shocks bias downward (-5 to -25¢), with ~20% positive shocks

    Normal ticks between shocks are smoothed to MAX_NORMAL_JUMP = 6¢.

    Parameters
    ----------
    entry_price_cents : int
        Starting price (our entry).
    settlement_value : int
        100 (bracket wins) or 0 (bracket loses).
    total_minutes : int
        Duration from entry to settlement (default 12h = 720 min).
    tick_interval_minutes : int
        Granularity (default 5 min, matches position monitor cycle).
    seed : int
        RNG seed for reproducibility.
    """
    rng = np.random.default_rng(seed)
    n_ticks = total_minutes // tick_interval_minutes + 1
    t = np.linspace(0.0, 1.0, n_ticks)

    x0 = float(entry_price_cents)
    xT = 99.0 if settlement_value == 100 else 3.0
    sigma = 15.0 if settlement_value == 100 else 10.0

    # Build Brownian bridge
    Z = rng.standard_normal(n_ticks)
    bridge = np.empty(n_ticks)
    bridge[0] = x0
    bridge[-1] = xT

    for i in range(1, n_ticks - 1):
        ti = t[i]
        mean_t = x0 * (1.0 - ti) + xT * ti
        std_t = sigma * math.sqrt(ti * (1.0 - ti))
        bridge[i] = mean_t + std_t * Z[i]

    # For losing trades: 30% chance of brief spike (hope before collapse)
    if settlement_value == 0 and rng.random() < 0.30:
        spike_peak_tick = max(1, int(n_ticks * 0.15))
        spike_height = rng.integers(5, max(6, min(30, entry_price_cents)))
        for i in range(min(spike_peak_tick * 2, n_ticks)):
            spike_factor = 1.0 - abs(i - spike_peak_tick) / spike_peak_tick
            bridge[i] = bridge[i] + float(spike_height) * max(0.0, spike_factor)

    # ── Observation-release shocks ──
    # Real markets get 2-4 major information events per 12-hour window:
    # DSM, 6-hour obs, HRRR model run.  These cause gradual 10-25¢ moves
    # spread over 2-4 ticks (10-20 minutes as market digests the data).
    # This matches the position_monitor's 5-min cycle — momentum can
    # catch a drop as it develops, not after it's already happened.
    n_shocks = rng.integers(2, 5)  # 2 to 4 shocks
    # Place shocks in the middle 70% of the path (not start/end)
    shock_zone_start = max(4, int(n_ticks * 0.10))
    shock_zone_end = max(shock_zone_start + n_shocks + 4, int(n_ticks * 0.80))
    shock_starts = sorted(rng.choice(
        range(shock_zone_start, shock_zone_end),
        size=min(n_shocks, shock_zone_end - shock_zone_start),
        replace=False,
    ).tolist())

    # Collect all ticks that are part of a shock ramp
    shock_ticks = set()

    for st in shock_starts:
        total_magnitude = int(rng.integers(12, 26))  # 12-25¢ total move
        if settlement_value == 100:
            # Winners: 80% positive shocks, 20% negative (bad obs surprise)
            direction = 1 if rng.random() < 0.80 else -1
        else:
            # Losers: 80% negative shocks, 20% positive (false hope)
            direction = -1 if rng.random() < 0.80 else 1

        # Spread the shock over 3 ticks (15 minutes) as market digests data
        # This gives momentum a realistic window to detect the move
        ramp_ticks = rng.integers(2, 4)  # 2-3 ticks ramp
        per_tick = total_magnitude / ramp_ticks
        for offset in range(ramp_ticks):
            idx = st + offset
            if 0 < idx < n_ticks - 1:
                bridge[idx] = bridge[idx] + direction * per_tick * (1.0 - 0.2 * offset)
                shock_ticks.add(idx)

    # ── Smoothing ──
    # Normal ticks: limit to MAX_NORMAL_JUMP (realistic quiet-market drift).
    # Shock ticks: allow up to MAX_SHOCK_JUMP (obs-release ramp, per-tick).
    MAX_NORMAL_JUMP = 6
    MAX_SHOCK_JUMP = 15  # Per-tick during shock ramp (realistic for 5-min)

    for i in range(1, len(bridge)):
        delta = bridge[i] - bridge[i - 1]
        limit = MAX_SHOCK_JUMP if i in shock_ticks else MAX_NORMAL_JUMP
        if abs(delta) > limit:
            bridge[i] = bridge[i - 1] + limit * np.sign(delta)

    # Clip to valid range
    bridge = np.clip(bridge, 1, 99).astype(int)

    # Convert to PriceTick objects
    hours_total = total_minutes / 60.0
    ticks = []
    for i in range(n_ticks):
        minutes = i * tick_interval_minutes
        hours_to_settle = max(0.0, hours_total - minutes / 60.0)
        ticks.append(
            PriceTick(
                minutes_from_entry=minutes,
                price_cents=int(bridge[i]),
                hours_to_settlement=hours_to_settle,
            )
        )
    return ticks


# ═══════════════════════════════════════════════════════════════
#  EXIT RULE SIMULATION ENGINE
# ═══════════════════════════════════════════════════════════════


def simulate_exit_rules(
    price_path: list[PriceTick],
    entry_price_cents: int,
    initial_contracts: int,
    strategy: StrategyConfig,
    settlement_value: int,
) -> list[ExitEvent]:
    """Run exit rules against a price path, return triggered exits.

    Evaluates rules in exact position_monitor order:
      1. Efficiency exit (≥ 90¢)
      2. Freeroll (price ≥ mult × entry, not already freerolled, qty > 1)
      3. Mid-profit (post-freeroll, ≥ threshold, not already taken, qty > 1)
      4. Trailing stop (post-freeroll, price ≤ floor)
      5. Momentum drop (≥ 15¢ drop in one tick, pre-freeroll)

    Remaining contracts settle at the known outcome.
    """
    contracts = initial_contracts
    freerolled = False
    mid_profit_taken = False
    peak_price = entry_price_cents
    trailing_floor = 0
    prev_price = entry_price_cents
    exit_events: list[ExitEvent] = []

    for tick in price_path:
        price = tick.price_cents
        hours = tick.hours_to_settlement

        if contracts <= 0:
            break

        # ── EXIT 1: EFFICIENCY (90¢) ──
        if strategy.efficiency_exit_enabled and price >= strategy.efficiency_threshold:
            exit_events.append(
                ExitEvent(
                    exit_type="efficiency",
                    exit_price_cents=price,
                    minutes_from_entry=tick.minutes_from_entry,
                    contracts_sold=contracts,
                    contracts_remaining=0,
                    pnl_cents=(price - entry_price_cents) * contracts,
                )
            )
            contracts = 0
            break

        # ── EXIT 2: FREEROLL ──
        if strategy.freeroll_enabled and not freerolled and contracts > 1:
            if strategy.adaptive_freeroll_enabled:
                base_mult = _adaptive_freeroll_multiplier(entry_price_cents)
            else:
                base_mult = strategy.freeroll_multiplier

            if strategy.time_decay_enabled:
                effective_mult = _scaled_freeroll_multiplier(base_mult, hours)
            else:
                effective_mult = base_mult

            freeroll_price = entry_price_cents * effective_mult

            if price >= freeroll_price:
                sell_qty = contracts // 2
                exit_events.append(
                    ExitEvent(
                        exit_type="freeroll",
                        exit_price_cents=price,
                        minutes_from_entry=tick.minutes_from_entry,
                        contracts_sold=sell_qty,
                        contracts_remaining=contracts - sell_qty,
                        pnl_cents=(price - entry_price_cents) * sell_qty,
                    )
                )
                contracts -= sell_qty
                freerolled = True
                peak_price = price
                offset = _trailing_offset_for_price(price)
                trailing_floor = max(entry_price_cents, price - offset)

        # ── EXIT 2.5: MID-PROFIT ──
        if (
            strategy.mid_profit_enabled
            and freerolled
            and not mid_profit_taken
            and contracts > 1
        ):
            if strategy.time_decay_enabled:
                threshold = _scaled_mid_profit_threshold(
                    strategy.mid_profit_threshold, hours
                )
            else:
                threshold = strategy.mid_profit_threshold

            if price >= threshold:
                sell_qty = max(1, int(contracts * MID_PROFIT_SELL_FRACTION))
                exit_events.append(
                    ExitEvent(
                        exit_type="mid_profit",
                        exit_price_cents=price,
                        minutes_from_entry=tick.minutes_from_entry,
                        contracts_sold=sell_qty,
                        contracts_remaining=contracts - sell_qty,
                        pnl_cents=(price - entry_price_cents) * sell_qty,
                    )
                )
                contracts -= sell_qty
                mid_profit_taken = True

        # ── EXIT 3: TRAILING STOP ──
        if strategy.trailing_stop_enabled and freerolled and contracts > 0:
            base_offset = _trailing_offset_for_price(price)
            if strategy.time_decay_enabled:
                current_offset = _scaled_trailing_offset(base_offset, hours)
            else:
                current_offset = base_offset

            if price > peak_price:
                peak_price = price
                new_floor = max(entry_price_cents, price - current_offset)
                trailing_floor = max(trailing_floor, new_floor)

            if trailing_floor > 0 and price <= trailing_floor:
                exit_events.append(
                    ExitEvent(
                        exit_type="trailing",
                        exit_price_cents=price,
                        minutes_from_entry=tick.minutes_from_entry,
                        contracts_sold=contracts,
                        contracts_remaining=0,
                        pnl_cents=(price - entry_price_cents) * contracts,
                    )
                )
                contracts = 0
                break

        # ── EXIT 3.5: MOMENTUM ──
        if strategy.momentum_enabled and not freerolled:
            is_drop, _drop_amt = _check_momentum_drop(price, prev_price)
            if is_drop:
                exit_events.append(
                    ExitEvent(
                        exit_type="momentum",
                        exit_price_cents=price,
                        minutes_from_entry=tick.minutes_from_entry,
                        contracts_sold=contracts,
                        contracts_remaining=0,
                        pnl_cents=(price - entry_price_cents) * contracts,
                    )
                )
                contracts = 0
                break

        prev_price = price

    # ── Remaining contracts settle ──
    if contracts > 0:
        exit_events.append(
            ExitEvent(
                exit_type="hold_to_settle",
                exit_price_cents=settlement_value,
                minutes_from_entry=price_path[-1].minutes_from_entry if price_path else 0,
                contracts_sold=contracts,
                contracts_remaining=0,
                pnl_cents=(settlement_value - entry_price_cents) * contracts,
            )
        )

    return exit_events


# ═══════════════════════════════════════════════════════════════
#  BACKTEST RUNNER
# ═══════════════════════════════════════════════════════════════


def _parse_brackets(record: dict) -> list[BracketInfo]:
    """Extract between-type brackets from a settlement record."""
    brackets = []
    for s in record.get("settlements", []):
        if s.get("strike_type") != "between":
            continue
        floor_s = s.get("floor_strike")
        cap_s = s.get("cap_strike")
        if floor_s is None or cap_s is None:
            continue
        brackets.append(
            BracketInfo(
                ticker=s.get("ticker", ""),
                floor_strike=float(floor_s),
                cap_strike=float(cap_s),
                strike_type="between",
                result=s.get("result", "no"),
                yes_bid_close=int(s.get("yes_bid_close", 0)),
                volume=int(s.get("volume", 0)),
            )
        )
    return brackets


def _make_seed(base_seed: int, date_str: str, city: str, ticker: str, entry: int) -> int:
    """Deterministic seed for reproducibility per-trade."""
    h = hash((date_str, city, ticker, entry))
    return abs(base_seed + h) % (2**31)


def run_backtest(
    city_filter: str | None = None,
    date_start: str | None = None,
    date_end: str | None = None,
    entry_prices: list[int] | None = None,
    contracts_per_trade: int = 10,
    strategies: list[str] | None = None,
    seed: int = 42,
    edge_only: bool = False,
    winners_only: bool = False,
) -> dict[str, list[SimulatedTrade]]:
    """Run full backtest across all specified strategies.

    Parameters
    ----------
    edge_only : bool
        If True, only simulate the winning bracket + its immediate neighbors.
        This approximates the real system's 90+ confidence gate: we only
        enter brackets where our ensemble model identified edge.
    winners_only : bool
        If True, only simulate the bracket that actually won. This answers:
        "Given we picked the right bracket, do exits help or hurt?"
        Implies edge_only=True. Most closely models our 90+ confidence gate
        since the real system rarely enters a losing bracket at high confidence.

    Returns dict of strategy_name → list[SimulatedTrade].
    """
    if entry_prices is None:
        entry_prices = [10, 15, 20, 25, 30]
    if strategies is None:
        strategies = list(STRATEGY_PRESETS.keys())

    # Load data
    records = load_records(city_filter)
    if not records:
        print("No records found.")
        return {}

    # Filter by date range
    if date_start:
        records = [r for r in records if r.get("date", "") >= date_start]
    if date_end:
        records = [r for r in records if r.get("date", "") <= date_end]

    # Filter to records with actual_high and brackets
    records = [
        r
        for r in records
        if r.get("actual_high") is not None and len(r.get("settlements", [])) >= 2
    ]

    results: dict[str, list[SimulatedTrade]] = {s: [] for s in strategies}

    for record in records:
        brackets = _parse_brackets(record)
        if not brackets:
            continue

        date_str = record["date"]
        city = record["city"]
        actual_high = record["actual_high"]

        # If edge_only or winners_only, narrow down the bracket set
        if edge_only or winners_only:
            winning_idx = None
            for i, b in enumerate(brackets):
                if b.floor_strike <= actual_high < b.cap_strike:
                    winning_idx = i
                    break
            if winning_idx is None:
                continue  # actual_high outside all brackets (tail)

            if winners_only:
                # Only simulate the bracket that actually won
                brackets = [brackets[winning_idx]]
            else:
                # Simulate winning bracket + one on each side
                indices = set()
                indices.add(winning_idx)
                if winning_idx > 0:
                    indices.add(winning_idx - 1)
                if winning_idx < len(brackets) - 1:
                    indices.add(winning_idx + 1)
                brackets = [brackets[i] for i in sorted(indices)]

        for bracket in brackets:
            # Determine settlement: did actual_high fall in this bracket?
            won = bracket.floor_strike <= actual_high < bracket.cap_strike
            settlement_value = 100 if won else 0

            for entry_price in entry_prices:
                # Entry filter: don't simulate impossible entries
                # For winners: entry must be ≤ closing price (we bought before it rallied)
                # For losers: any entry is realistic (market was uncertain)
                if won and entry_price > bracket.yes_bid_close and bracket.yes_bid_close > 0:
                    continue
                # Skip if entry >= 50 (our max entry rule)
                if entry_price > 50:
                    continue
                # Skip brackets with zero volume
                if bracket.volume == 0:
                    continue

                trade_seed = _make_seed(seed, date_str, city, bracket.ticker, entry_price)

                # Generate price path once (shared across strategies for fair comparison)
                path = simulate_price_path(
                    entry_price_cents=entry_price,
                    settlement_value=settlement_value,
                    seed=trade_seed,
                )

                hold_pnl = (settlement_value - entry_price) * contracts_per_trade

                for strategy_name in strategies:
                    strategy = STRATEGY_PRESETS[strategy_name]

                    exits = simulate_exit_rules(
                        price_path=path,
                        entry_price_cents=entry_price,
                        initial_contracts=contracts_per_trade,
                        strategy=strategy,
                        settlement_value=settlement_value,
                    )

                    total_pnl = sum(e.pnl_cents for e in exits)
                    peak = max((e.exit_price_cents for e in exits), default=entry_price)

                    trade = SimulatedTrade(
                        date=date_str,
                        city=city,
                        ticker=bracket.ticker,
                        bracket_low=bracket.floor_strike,
                        bracket_high=bracket.cap_strike,
                        side="yes",
                        entry_price_cents=entry_price,
                        settlement_value=settlement_value,
                        contracts=contracts_per_trade,
                        exit_events=exits,
                        total_pnl_cents=total_pnl,
                        hold_to_settle_pnl=hold_pnl,
                        peak_price=peak,
                        strategy_name=strategy_name,
                    )
                    results[strategy_name].append(trade)

    return results


# ═══════════════════════════════════════════════════════════════
#  REPORT GENERATION
# ═══════════════════════════════════════════════════════════════


def generate_report(results: dict[str, list[SimulatedTrade]]) -> list[StrategyReport]:
    """Compute aggregate stats for each strategy."""
    reports = []

    for strategy_name, trades in results.items():
        if not trades:
            reports.append(StrategyReport(strategy_name=strategy_name))
            continue

        total = len(trades)
        winners = sum(1 for t in trades if t.total_pnl_cents > 0)
        total_pnl = sum(t.total_pnl_cents for t in trades)
        hold_pnl = sum(t.hold_to_settle_pnl for t in trades)

        # Max drawdown (cumulative P&L trough)
        cum_pnl = 0.0
        peak_cum = 0.0
        max_dd = 0.0
        for t in sorted(trades, key=lambda x: x.date):
            cum_pnl += t.total_pnl_cents
            peak_cum = max(peak_cum, cum_pnl)
            dd = peak_cum - cum_pnl
            max_dd = max(max_dd, dd)

        # Sharpe ratio (daily P&L)
        daily_pnl: dict[str, float] = defaultdict(float)
        for t in trades:
            daily_pnl[t.date] += t.total_pnl_cents
        daily_vals = list(daily_pnl.values())
        if len(daily_vals) > 1:
            mean_d = np.mean(daily_vals)
            std_d = np.std(daily_vals, ddof=1)
            sharpe = float(mean_d / std_d * np.sqrt(252)) if std_d > 0 else 0.0
        else:
            sharpe = 0.0

        # Exit type distribution
        exit_counts: dict[str, int] = defaultdict(int)
        exit_price_sums: dict[str, float] = defaultdict(float)
        for t in trades:
            for e in t.exit_events:
                exit_counts[e.exit_type] += e.contracts_sold
                exit_price_sums[e.exit_type] += e.exit_price_cents * e.contracts_sold

        avg_exit_price = {}
        for etype, count in exit_counts.items():
            avg_exit_price[etype] = exit_price_sums[etype] / count if count > 0 else 0

        # Per entry-price breakdown
        pnl_by_entry: dict[int, list[float]] = defaultdict(list)
        wins_by_entry: dict[int, list[bool]] = defaultdict(list)
        for t in trades:
            pnl_by_entry[t.entry_price_cents].append(t.total_pnl_cents)
            wins_by_entry[t.entry_price_cents].append(t.total_pnl_cents > 0)

        pnl_by_entry_avg = {k: np.mean(v) for k, v in pnl_by_entry.items()}
        winrate_by_entry = {
            k: sum(v) / len(v) * 100 for k, v in wins_by_entry.items()
        }

        reports.append(
            StrategyReport(
                strategy_name=strategy_name,
                total_trades=total,
                winning_trades=winners,
                win_rate=winners / total * 100 if total > 0 else 0,
                total_pnl_cents=total_pnl,
                avg_pnl_per_trade=total_pnl / total if total > 0 else 0,
                max_drawdown_cents=max_dd,
                sharpe_ratio=sharpe,
                exit_counts=dict(exit_counts),
                avg_exit_price=avg_exit_price,
                pnl_vs_hold=total_pnl - hold_pnl,
                pnl_by_entry_price=pnl_by_entry_avg,
                winrate_by_entry_price=winrate_by_entry,
            )
        )

    return reports


def print_report(reports: list[StrategyReport], city: str = "ALL", days: str = "all") -> None:
    """Pretty-print comparison table to stdout."""
    print()
    print("═" * 80)
    print(f"  BACKTEST RESULTS — {city}, {days} days")
    print("═" * 80)

    # Strategy comparison table
    print()
    print(
        f"  {'Strategy':<28s} {'Trades':>7s} {'WinRate':>8s} "
        f"{'Total P&L':>10s} {'Avg/Trade':>10s} {'MaxDD':>8s} {'Sharpe':>7s}"
    )
    print("  " + "─" * 78)

    for r in reports:
        pnl_str = f"${r.total_pnl_cents / 100:+,.0f}" if r.total_trades > 0 else "—"
        avg_str = f"{r.avg_pnl_per_trade:+.1f}¢" if r.total_trades > 0 else "—"
        dd_str = f"${r.max_drawdown_cents / 100:,.0f}" if r.total_trades > 0 else "—"
        sharpe_str = f"{r.sharpe_ratio:.2f}" if r.total_trades > 0 else "—"
        wr_str = f"{r.win_rate:.1f}%" if r.total_trades > 0 else "—"

        print(
            f"  {r.strategy_name:<28s} {r.total_trades:>7,d} {wr_str:>8s} "
            f"{pnl_str:>10s} {avg_str:>10s} {dd_str:>8s} {sharpe_str:>7s}"
        )

    # Delta vs hold-to-settlement
    hold_report = next((r for r in reports if "Hold" in r.strategy_name), None)
    if hold_report and hold_report.total_trades > 0:
        print()
        print("  VALUE ADDED vs HOLD-TO-SETTLEMENT:")
        for r in reports:
            if "Hold" in r.strategy_name or r.total_trades == 0:
                continue
            delta = r.pnl_vs_hold
            print(f"    {r.strategy_name:<28s} {delta / 100:+,.0f}$")

    # Exit distribution for "all_upgrades" or last strategy
    target = next((r for r in reports if "All" in r.strategy_name), reports[-1] if reports else None)
    if target and target.exit_counts:
        total_contracts = sum(target.exit_counts.values())
        print()
        print(f"  EXIT DISTRIBUTION ({target.strategy_name}):")
        for etype in ["freeroll", "efficiency", "trailing", "mid_profit", "momentum", "hold_to_settle"]:
            count = target.exit_counts.get(etype, 0)
            pct = count / total_contracts * 100 if total_contracts > 0 else 0
            avg_p = target.avg_exit_price.get(etype, 0)
            print(f"    {etype:<18s} {count:>7,d} contracts ({pct:5.1f}%)  avg exit: {avg_p:.0f}¢")

    # P&L by entry price
    if target and target.pnl_by_entry_price:
        print()
        print(f"  P&L BY ENTRY PRICE ({target.strategy_name}):")
        for entry in sorted(target.pnl_by_entry_price.keys()):
            avg_pnl = target.pnl_by_entry_price[entry]
            wr = target.winrate_by_entry_price.get(entry, 0)
            print(f"    {entry:>3d}¢ entry: {avg_pnl:+6.1f}¢/trade  (win rate: {wr:.0f}%)")

    print()
    print("═" * 80)


# ═══════════════════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════════════════


def main():
    parser = argparse.ArgumentParser(
        description="Backtest Simulator — Exit strategy evaluation via Brownian bridge",
    )
    parser.add_argument("--city", type=str, default=None, help="Filter by city (NYC, CHI, DEN, MIA, LAX)")
    parser.add_argument("--days", type=int, default=None, help="Limit to last N days")
    parser.add_argument(
        "--entry",
        type=int,
        nargs="+",
        default=[10, 15, 20, 25, 30],
        help="Entry price(s) to simulate (cents)",
    )
    parser.add_argument("--contracts", type=int, default=10, help="Contracts per trade (default: 10)")
    parser.add_argument(
        "--strategy",
        type=str,
        nargs="+",
        default=None,
        help=f"Strategy presets to run (choices: {', '.join(STRATEGY_PRESETS.keys())})",
    )
    parser.add_argument("--compare", action="store_true", help="Run all strategies (same as default)")
    parser.add_argument(
        "--edge-only",
        action="store_true",
        help="Only simulate winning bracket + neighbors (approximates 90+ confidence targeting)",
    )
    parser.add_argument(
        "--winners-only",
        action="store_true",
        help="Only simulate brackets that actually won (pure winner P&L analysis)",
    )
    parser.add_argument("--seed", type=int, default=42, help="Random seed for reproducibility")
    parser.add_argument("--json", action="store_true", help="Output results as JSON")
    args = parser.parse_args()

    date_start = None
    date_end = None
    days_label = "all"
    if args.days:
        end = date.today()
        start = end - timedelta(days=args.days)
        date_start = start.isoformat()
        date_end = end.isoformat()
        days_label = str(args.days)

    strategy_names = args.strategy or list(STRATEGY_PRESETS.keys())

    mode = "winners-only" if args.winners_only else ("edge-only" if args.edge_only else "all-brackets")
    print(f"Loading data... (city={args.city or 'ALL'}, days={days_label}, mode={mode})")
    results = run_backtest(
        city_filter=args.city,
        date_start=date_start,
        date_end=date_end,
        entry_prices=args.entry,
        contracts_per_trade=args.contracts,
        strategies=strategy_names,
        seed=args.seed,
        edge_only=args.edge_only,
        winners_only=args.winners_only,
    )

    total_trades = sum(len(v) for v in results.values())
    print(f"Simulated {total_trades:,d} trades across {len(strategy_names)} strategies.")

    reports = generate_report(results)

    if args.json:
        import dataclasses

        out = []
        for r in reports:
            d = dataclasses.asdict(r)
            # Convert numpy types to native Python for JSON
            for k, v in d.items():
                if isinstance(v, dict):
                    d[k] = {str(kk): float(vv) if hasattr(vv, "item") else vv for kk, vv in v.items()}
                elif hasattr(v, "item"):
                    d[k] = float(v)
            out.append(d)
        print(json.dumps(out, indent=2))
    else:
        print_report(reports, city=args.city or "ALL", days=days_label)


if __name__ == "__main__":
    main()
