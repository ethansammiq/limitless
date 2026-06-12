# Paper Trading

Run the scanners and trading loop with simulated execution against **real
Kalshi market quotes**. No real capital, no real orders, full telemetry.

## Quickstart

```bash
# One-off dry run: scan policy markets, log what would trade, place nothing
PAPER_TRADING_MODE=true python3 -m markets.policy.trader --dry-run

# Paper scan + paper order placement (fills simulated vs real quotes)
PAPER_TRADING_MODE=true python3 -m markets.policy.trader

# Weather auto-trader in paper mode (same plumbing, same telemetry)
PAPER_TRADING_MODE=true python3 auto_trader.py

# Position monitor honors paper mode too
PAPER_TRADING_MODE=true python3 position_monitor.py
```

Set `PAPER_TRADING_MODE` in your `.env` if you want the default to be paper.

## Environment variables

| Var | Default | Meaning |
|---|---|---|
| `PAPER_TRADING_MODE` | `false` | When `true`, routes all order placement through `PaperBroker` |
| `PAPER_INITIAL_BALANCE` | `1000.0` | Starting paper bankroll in dollars |
| `PAPER_FILL_MODE` | `resting` | `resting`: orders rest; fill when real quote touches the limit. `instant`: cross-or-reject. |
| `ANTHROPIC_API_KEY` | — | Required for the LLM synthesizer |
| `CONGRESS_GOV_API_KEY` | — | Required for the policy source adapter (free at api.data.gov) |

## State files

Paper and live state are **strictly separated**. You cannot corrupt one with
the other.

| File | Purpose |
|---|---|
| `positions.json` | Live positions — untouched when paper mode is on |
| `positions_paper.json` | Paper positions |
| `paper_balance.json` | Paper balance (starts at `PAPER_INITIAL_BALANCE`) |
| `paper_orders.json` | All paper orders: resting, executed, canceled |
| `.positions.lock` / `.positions_paper.lock` | fcntl locks |

## Resetting paper state

```bash
PAPER_TRADING_MODE=true python3 scripts/reset_paper.py
```

Deletes the four paper files above. Live files are never touched. The script
refuses to run unless `PAPER_TRADING_MODE=true` is set (or `--force` is
passed), preventing accidental invocation from corrupting real portfolios.

## Fill modes

### `resting` (default, recommended)

A limit order rests until a real Kalshi quote touches its price, then fills.
Matches the `bid+1` maker-only strategy the scanners use. Order sweeps happen
opportunistically whenever any code calls `get_orderbook`, `get_positions`, or
`get_orders` — so the fill logic piggybacks on the normal scanner cadence.

### `instant`

A limit order either fills immediately (if it would cross the current book)
or is rejected. Faster to iterate on, but not representative of real market
behavior. Useful for fast unit tests.

## What's simulated vs real

| Component | Real | Simulated |
|---|---|---|
| Market listings (`get_markets`) | ✓ | |
| Orderbooks (`get_orderbook`) | ✓ | |
| Balance | | ✓ |
| Positions | | ✓ |
| Order placement (`place_order`) | | ✓ (vs real quotes) |
| Order cancellation | | ✓ |
| Fills (`get_fills`) | | ✓ |

This means your P&L is **measured against true market prices** — when you
close a paper position the credit you receive is the real Kalshi bid at the
time of the sell. Settlement is against the real Kalshi resolution.

## Calibration telemetry

Paper trades emit the same events as live trades (`trade_events.log_event`,
`outcome_tracker.log_trade_prediction`, scanner logs). The only difference is
the `broker_mode: "paper"` field on trade events. Compare paper Brier score
to real Brier score to decide whether the edge is real.

## Gotchas

- The `PaperBroker`'s `quote_client` is a `KalshiClient` without credentials.
  Kalshi's `/markets` and `/markets/{ticker}/orderbook` endpoints are public,
  so this works without an API key.
- If you run the policy trader without `ANTHROPIC_API_KEY` or
  `CONGRESS_GOV_API_KEY`, the scanner will report zero tradeable
  opportunities (the LLM synth / Congress.gov adapter returns empty/failure).
  That's expected — set the keys when you want live synthesis.
- The `.env` file must include any keys you want loaded. `dotenv` is
  auto-called at module top.
