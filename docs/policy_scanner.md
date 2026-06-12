# Policy Scanner

Scans low-liquidity Kalshi policy/legislative markets, synthesizes calibrated
probabilities from Congress.gov primary sources via Claude Opus 4.7, and
opens trades only when the LLM's probability diverges from the market's by
more than the configured threshold.

## Pipeline

```
  Kalshi /markets  ──►  prefilter  ──►  Congress.gov adapter  ──►  LLM synth  ──►  divergence gate  ──►  DocOpportunity
     │                  (series +         (fresh-doc            (Opus 4.7       (≥10pp, conf≥MED)       │
     │                   volume +          within N days,        structured                             ▼
     ▼                   hours cap)        token-overlap         JSON via tool                      trader.py
  Broker factory                           matching)             use)                                (paper/live)
  (paper or live)
```

## Files

| File | Purpose |
|---|---|
| `markets/policy/config.py` | Series-ticker whitelist + `is_policy_market()` utility |
| `markets/policy/sources/congress_gov.py` | `api.congress.gov` adapter + `DocBundle` |
| `markets/policy/scanner.py` | Filter → fetch → synth → divergence gate |
| `markets/policy/trader.py` | CLI entry point |
| `core/llm_synth.py` | Claude Opus 4.7 structured-output synthesizer |
| `core/opportunity.py` | `DocOpportunity` dataclass and `OpportunityBase` protocol |

## Series whitelist

Matched by ticker prefix. See `markets/policy/config.POLICY_SERIES_PREFIXES`.
Covers: confirmations, bill passage, roll-call votes, nominations, House
markets, shutdown markets, Federal Register rules, appropriations, debt
ceiling, tariffs, approval ratings, "Will X sign?" markets, and "Will X
pass?" markets.

New series prefixes should be added here after verifying they exist on
Kalshi's live listings.

## Thresholds (env-overridable, defaults in `config.py`)

| Var | Default | Purpose |
|---|---|---|
| `POLICY_SCAN_MAX_VOLUME` | `500000` | Max 24h volume for a market to qualify (avoid flagship/liquid contracts) |
| `POLICY_SCAN_MIN_HOURS_TO_SETTLE` | `48` | Minimum hours to settlement (let limit orders rest) |
| `POLICY_SCAN_FRESHNESS_DAYS` | `7` | Doc must have been updated within this window |
| `POLICY_DIVERGENCE_THRESHOLD_PP` | `10` | Minimum LLM-vs-market divergence in percentage points |
| `POLICY_MIN_LLM_CONFIDENCE` | `MEDIUM` | Floor for the LLM's self-reported confidence (`HIGH`/`MEDIUM`/`LOW`) |
| `POLICY_MAX_ENTRY_PRICE_CENTS` | `60` | Refuse entries above this price on either side |
| `LLM_SYNTH_MODEL` | `claude-opus-4-7` | Anthropic model ID |
| `LLM_SYNTH_MAX_TOKENS` | `2048` | Output cap |
| `LLM_SYNTH_TIMEOUT_SEC` | `120` | Per-call timeout |

Tune these based on paper-trading Brier scores. Tightening divergence and
confidence gates is the main safety knob when the LLM is noisy.

## Sizing

Half-Kelly, capped at 10% of bankroll per trade. See `_half_kelly_size` in
`markets/policy/scanner.py`. Bankroll comes from `broker.get_balance()` at
scan time.

## Side selection

```
  yes_edge = llm_prob - (yes_bid / 100)
  no_edge  = (1 - llm_prob) - ((100 - yes_ask) / 100)

  if yes_edge >= no_edge and yes_edge > 0:
      buy YES @ yes_bid + 1
  elif no_edge > 0:
      buy NO  @ (100 - yes_ask) + 1
  else:
      skip
```

## Adding a new source adapter

An adapter needs two things:
1. Be importable from `markets/<domain>/sources/<name>.py`.
2. Expose a class with an async `fetch_fresh_doc(market, freshness_days)`
   method returning a `DocBundle` or `None`.

Example skeleton:

```python
from markets.policy.sources.congress_gov import DocBundle

class CourtListenerAdapter:
    async def start(self): ...
    async def stop(self): ...
    async def fetch_fresh_doc(self, market, freshness_days=7) -> DocBundle | None:
        # 1. Extract case id / docket from market['title']
        # 2. Hit courtlistener.com/api/rest/v4
        # 3. If docket updated within freshness_days, build a DocBundle
        # 4. Otherwise return None
        ...
```

The scanner is currently wired to `CongressGovAdapter` directly. Phase 2
will introduce an adapter registry keyed by series prefix so multiple
adapters can serve one scan.

## Calibration

Every tradeable opportunity is emitted with:
- `llm_prob`, `llm_confidence_tier`, `llm_reasoning`
- `market_implied_prob`, `divergence_pp`
- `source_adapter`, `source_urls`, `doc_last_updated`
- `supporting_facts`, `opposing_facts`

Downstream telemetry (`trade_events.log_event`, `outcome_tracker`) captures
these fields so the Brier score of `llm_prob` vs actual resolution can be
computed post-settlement. See [PAPER_TRADING.md](../PAPER_TRADING.md).

## Known limitations (Phase 1)

1. **Single-model LLM.** Opus 4.7 is the sole synthesizer. Phase 2 adds
   ensemble via OpenRouter (GPT-4o + DeepSeek).
2. **Heuristic ticker→doc matching.** Token overlap with a 0.35 minimum
   score. Will miss markets whose titles don't echo the bill/nomination
   title. Phase 2 adds an explicit ticker-prefix to source-endpoint map.
3. **No trading_guards wrapped for policy yet.** Weather guards check DSM
   windows and city-correlation — both irrelevant here. Kill switch is
   honored. Policy-specific guards (e.g. per-Congress exposure) to come.
4. **Settlement check not automated.** When a paper position's market
   settles on Kalshi, P&L reconciliation requires a manual script pass.
   Phase 2 adds a settlement cron.
