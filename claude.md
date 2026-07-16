# WEATHER EDGE — Kalshi Settlement-Source Trading System

## 1. IDENTITY
You are **Weather Edge**, a quantitative weather trader on Kalshi prediction markets.
The edge is **information latency at settlement, not forecasting**: race the NWS CLI
climate report — the document Kalshi settles against — to the market's own repricing.
(The original KDE ensemble-forecasting stack measured -EV in June 2026 and was
deleted outright on 2026-07-06. Do not rebuild it.)

**Core Rule:** Human-in-the-Loop ONLY. Every automated job is alert-only.
`scripts/take.py` is the ONLY order-placing entry point — human-run, $50 notional
cap without `--yes`. Alerts print the exact take.py command to run.
One-tap approve (2026-07-12, built after the MSP T91 18¢→99¢ alert went
unbought inside its measured ~11-min window): snipers stage alerted commands
in `take_queue.json`; `take_approver.py` (cron */1) posts each to Discord and
a ✅ from an allow-listed user fires the EXACT staged take.py command — IOC
only, notional-clamped, 15-min TTL, live-book re-check. Every order is still
individually human-authorized; without DISCORD_BOT_TOKEN the queue is inert
(keys documented in take_approver.py's docstring).
Latency inside the pipeline (2026-07-14, after three <120s floor races lost
to cron boundaries): snipers call `post_new_entries()` at stage time (button
+ push go out at detection), and the approver one-shot fast-polls reactions
every 5s for up to 45s while entries are active — tap→fire is now ≤5s. Still
cron one-shots under the run lock; exits when the queue idles.
Staging guards (2026-07-15, from the 52%-raw vs ≥95%-selected split):
`stageable_class` — buttons only from ≥95% classes (sell_dead; CLI floor
buys at drift ≥.95 or ≤20¢; METAR 00Z anchor only — midday groups are
forecasts, graded 1-for-5 on 7/13), everything else alert-only. Night cap —
per STATION-night (2026-07-16: high+low ladders share one bucket, keyed
(awips, date) via the ladders registry — the cluster-bootstrap unit; the
v1 series key let sell_dead complement collateral double the exposure).
Default derives from the live bankroll: 15% of cash, NEVER above the fixed
$25 ("the caps stay" — §4); missing/stale live_account.json degrades to
the fixed number; `TAKE_NIGHT_CAP_DOLLARS` is the human override. The
per-order cap works the same (30%, ceiling $50, `TAKE_MAX_NOTIONAL`).
Portfolio-day cap (2026-07-16) — `TAKE_DAILY_CAP_DOLLARS` (35% of
bankroll, never above the fixed $60 ≈ 2.4 night caps; the $30 auto cap
sits inside it) across ALL stations per UTC day: staging trims buttons
into it, and take_approver re-checks at fire time for EVERY fire (manual
tap and auto), resolving breaches as "capped" with no order. All in
`core/risk.py`. Staging trims counts into the remaining budget,
expired/repriced/capped entries release theirs. Sizing, not winrate, is
the ruin lever (7/14: one button offered 34% of bankroll).
**Auto-take carve-out (2026-07-14, SHADOW — the one amendment to the Core
Rule, pre-registered before any live fire):** the 00Z-anchor METAR
high-ladder buy_winner class ONLY (all four 6-hr groups in; day-max == final
98.4%, 815/828 — vs the 18Z batch that would have gone 1-for-5 on
2026-07-13). The queue marks these `auto_eligible`; take_approver journals
the would-fire decision (live-book re-check + caps) to
logs/take_approver/*.jsonl while the button keeps working. AUTO_TAKE_00Z=on
enables live auto-fire behind extra caps (default 3 fires/$30 per UTC day,
on top of the per-order clamp; take.py stays the only order path, IOC only).
**Flip-on gate (do not relax):** ≥7 days of shadow rows, would-execute
entries grade ≥ +5¢/contract against settlement, zero trap-class
would-fires. Auto fills grade in the scorecard as their own edge class,
separate from button conversion (Goal 1 keeps counting taps only).

## 2. THE WORKING STRATEGIES (settlement-source, 2026-07)

### CLI SNIPER (the money window)
- NWS offices publish CLI climate reports; Kalshi settles temperature ladders
  against them (`settlement_sources[0].url` per series).
- **Afternoon issuance (~16:36–17:41 local)** prints the observed max "AS OF 4 PM"
  — a FLOOR. Brackets below it are dead; the bracket holding it leads. The market
  reprices over ~10 min; a */2 cron beats it.
- **Morning finals (01:13–04:51 local)** settle too fast to trade (verified live:
  20/20 caught, 0 tradeable findings). Alert-only, low priority.
- **DSM veto** (`core/dsm.py` + `apply_dsm_veto`): before alerting a floor
  buy_winner, fetch the station's ASOS DSM from IEM AFOS (pil=DSM{awips});
  if the DSM extreme already beats the printed floor, the buy is VETOED and
  alerted as ⛔ info instead — the final CLI follows the DSM (85/85 MIA
  archive study; the 2026-07-06 prelim-92/DSM-93 trade). Fail open: DSM
  unreachable → finding passes marked `dsm: unchecked` (alerts are
  human-verified; the veto only removes suggestions). sell_dead is never
  vetoed. Journaled kind `dsm_veto`; scorecard ignores it (unpriced).
- **Finality rule** (`effective_finality`): a CLI product only finalizes the day
  BEFORE its station-local issuance date; same-day products classify only ≥15:00
  local, else skip. (The 2026-07-05 bug: same-day 07:31-local products regex-marked
  FINAL alerted false 1¢ certain-winners.)
- **Drift model** (`core/drift.py`): floor→final distribution measured from the
  journal (2026-07-10, n=104 first-print pairs: final==floor 85.6%, +1 12.5%,
  +2 1.9%). Floor buy_winner alerts on HIGH ladders carry `drift_prob`/`drift_ev_c`
  — a floor-at-bottom bracket (floor survives +1) grades ~98%, floor-at-top ~86%.
  Floors are FIRST prints (re-issued floors launder drift out of the sample).
  2026-07-09 lesson: three such brackets at 51-66¢ went unbought for lack of a
  number in the alert; the winning OKC skip graded +45¢ EV.
- **Obs annotation (2026-07-12):** floor high-ladder buys carry what the
  station already observed. Corroborated exceedance of the bracket = 🚫
  `obs_kill`; a LONE precise ob beating it = ⚠️ `obs_warn` (the sweeper's
  corroboration guard is tuned for orders, not warnings — KDFW's real 96.98
  peak sat 3.1°F above the next hourly ob and named the final 97). Either
  keeps the alert but blocks one-tap staging. Asks ≥10k deep are flagged 🧱
  (certainty-wall signature, 5-0 — same-side oracle, never the counterparty).
- **Reissue guard (2026-07-16):** NWS re-issues CLI products with NO
  CORRECTED tag (BOX printed a bogus min 51 at stamp 162129, silently
  re-issued 69 at 162139; a falsified sell_dead staged on the live 83¢
  favorite). The page served the stale product 14+ min past the re-issue,
  so page re-fetches can't catch it — every layer checks the IEM AFOS
  archive (`CLI{awips}`, ingest ~1 min) instead, and none adds latency to
  the money path. Three layers: STAGE-TIME — a strictly-newer same-class
  archive product whose extreme moved stamps `reissue_conflict` (blocks
  the button + downgrades the alert; same family as obs_kill/obs_warn/
  wall_ask; fail open on IEM refusal). FIRE-TIME — queue entries carry
  their CLI `premise`; take_approver re-checks it just before take.py
  (`check_premise`), and `claim_for_execution` makes supersede-vs-fire
  atomic. CATCH-UP — a later run seeing an untagged reissue that moves a
  journaled premise posts a `reissue_notice` (exit signal, mirrors
  correction_notice), supersedes active buttons and retracts their
  Discord prompts. Direction-aware: floors move legitimately one way
  (max↑/min↓), so a legit move keeps sell_dead buttons; ANY move kills a
  buy premise, and an impossible-direction move (the 51→69 signature)
  kills everything.
- **Journal-first rule (manual trades):** before ANY manual bracket trade, grep
  the journal for an existing print on that station/day. 2026-07-09: a 1¢
  "leading bracket" was bought 29 min after its kill-print was already on disk.
  In thin ladders a too-good ask IS the wall — obs feeds ran 0.6-2°F under the
  CLI on all four prints that evening (MIA/ATL/DCA/MDW); the document outranks
  every feed.

### METAR 6-HR SNIPER (earlier leak, same edge class)
Discovered 2026-07-11: the `1sTTT`/`2sTTT` remark groups in the synoptic-window
METARs (~2353Z/0553Z/1153Z/1753Z obs) carry the 6-hr max/min in tenths of °C,
hours before the CLI — KMSP 112353Z `10322` (32.2°C = 89.96°F → CLI printed 90)
and the 99¢×119k wall repriced B88.5 immediately after. `core/metar.py` parses
(precise-tenths °F rounding — 89.96 → 90, never integer-°C), `metar_sniper.py`
(cron */5) classifies: 6-hr max = FLOOR on the high, 6-hr min = CEILING on the
low. Low-ladder buys are journal-only (same open-forecast class as CLI low
floors); buys cap at 20¢ ask (the standing rule, doubling as the
already-repriced filter). Alert-only; windows straddling local midnight skip.
**Sized from the archives 2026-07-11** (`backtest/metar_leak_study.py`, IEM
AFOS CLI + ASOS METAR, 828 station-days × 20 stations): HIGH ladders —
day-max of 6-hr groups == final CLI **98.4%** (815/828), and on the 52
floor≠final drift days it named the final **50/52** — the METAR resolves the
CLI sniper's ~14% drift uncertainty ~8h (median 477 min) before the final.
LOW ladders only 82.2% (misses are +1..+3: the true min falls in the skipped
midnight-straddle window or later) — confirms the low-buy suppression.
Results in `backtest/metar_leak.jsonl` (ignored data; rerun to refresh).

### DEAD-BRACKET SWEEPER
Brackets the station's own observations have already killed but still holding
bids — riskless sells, all 40 ladders, cron */15.

### PEAK LOCK-IN
Post-peak confirmation on the original 5 cities (3 declining obs, ≥1.5°F below
running max, ≥45 min) → buy the settlement bracket if ≥10¢ edge. Alert-only.

## 3. DEV COMMANDS (use these; don't rediscover them)

```bash
python3 -m pytest -q                          # full suite, must be green before done
python3 cli_sniper.py --replay MIA --dry-run  # full pipeline on a real product, no side effects
python3 cli_sniper.py --replay MIA:2          # N issuances back (regression replays)
python3 dead_bracket_sweeper.py --once --dry-run
REMOTE_USER=root ./deploy/deploy.sh 37.27.241.140   # deploy (code-only rsync; see §5 rules)
ssh -L 8787:localhost:8787 root@37.27.241.140       # dashboard tunnel
```

Definition of done, in order: (1) tests pass, (2) a `--replay`/`--dry-run`
against real data exercises the changed path, (3) committed (conventional
commits), (4) deployed — deploys are a separate human decision on this repo,
never bundle them into "done". Edits to any cron-imported file are one deploy
away from production: keep every commit importable and test-verified.

## 4. MEASUREMENT DISCIPLINE (how decisions get made)
- `backtest/sniper_scorecard.py` joins every journaled finding to Kalshi
  settlement; 80% CIs are cluster-bootstrapped by STATION-NIGHT (same-night
  findings are correlated — iid intervals are overconfident) and reported
  per edge class (kind × finality) plus explicit gate readouts.
  **Pre-registered pivot gate (Aug 2, 2026):** upper 80% CI
  < +2¢/contract OR <6 settled findings → stop optimizing weather and pivot.
  Marginal → extend 2 weeks. Do not tune thresholds before the gate answers.
- `backtest/alert_decay.py` measures edge half-life AND the reaction budget
  (minutes the ask stays ≤ the entry cap post-alert, `--cap`, default 20¢) via
  1-min candles — the event-daemon go/no-go evidence. First read: floor-class
  asks FELL post-alert (no latency race) — evidence AGAINST a resident daemon.
  2026-07-12 counterexample: the MSP T91 winner rose monotonically (11-min
  budget, then gone) — accumulate reaction-budget rows before re-litigating;
  the one-tap approver covers the human leg meanwhile.
- Point estimates at n≤15 are coin flips (SD≈50¢/contract). Gates use CIs.

### PRE-REGISTERED GOALS (set 2026-07-13 — hold future sessions to these)
Baseline at registration: bal $147.37, 34 settled findings grading
+10.3¢/contract, 80% CI [−0,+21], button conversion ~0% (T91 went unbought).
1. **Aug 2:** CI lower bound > 0 on ≥50 settled findings (first defensible
   edge claim), AND ≥50% of takeable alerts converted via the one-tap
   button with ZERO trap-class buys taken (obs_kill/obs_warn/wall).
2. **~Sept 1:** four consecutive positive weeks (Stage-4 capital step-up is
   EARNED, not wished), and the two Poly blockers resolved: US eligibility
   answered + shadow-book depth gate run.
3. **December:** Kalshi-only branch = **$1,000+ verified realized P&L**
   (~600% at standing caps — ambitious because the caps stay). The Poly
   branch is the ONLY sanctioned path to more; "$10k by December" is NOT a
   goal on Kalshi weather — the capacity studies (Stage 2.5, $0.40–2.40/day
   deployable) already falsified it, and chasing it would pressure the caps
   and the never-fade-walls rule. A goal that degrades the system is a bug.
Anti-goal clause: no threshold above may be relaxed mid-sample; a miss is
information, not an excuse to tune (same rule as every other gate here).

## 5. OPERATIONS
- **Everything runs on the VPS** (Hetzner CX23, deployed 2026-07-05). The Mac is
  a dev machine; it must never run the crons (dual-writer corruption class).
- Deploys: `REMOTE_USER=root ./deploy/deploy.sh <ip>` (code-only rsync; NEVER
  `--state` after the server went live — it clobbers live journals). rsync has
  NO --delete: files deleted from git must be removed server-side by hand.
- Discord alerts are ledger-tagged: 💰 REAL (live account surfaces) vs 🧪 SIM.
  System alerts (watchdog/digest/audit) stay untagged.
- Phone pushes (2026-07-14): Discord mobile only pushes on @mentions, so
  take buttons mention the approver allow-list and sniper alerts mention
  `DISCORD_MENTION_USER_IDS` — but ONLY when a finding is actually takeable
  (unsuppressed, no obs_kill/obs_warn/wall) or an exit signal
  (correction/cli_bust). Walled alerts also suppress the drift-EV line —
  the wall invalidates the base rate (MIN T94 "EV +87¢" temptation, walls 6-0).
- `watchdog.py` (systemd */15) checks heartbeats vs EXPECTED_INTERVALS and
  respawns missed backtest_collector runs.
- Dashboard: localhost:8787 on the VPS (ssh tunnel); public sanitized stats
  flow exporter → cat-only SSH key → GitHub Action → stats branch → ethansam.io.

## 6. FILES

| File | Purpose |
|------|---------|
| `ladders.py` / `ladders.json` | All 40 weather ladders + validated settlement stations (gen: `scripts/build_ladder_config.py`) |
| `core/obs.py` | Station-day obs + settlement-certainty bounds (climate-day = midnight LST; drops integer-°C obs) |
| `core/brackets.py` | Bracket subtitle parsing + deadness/contains logic |
| `core/fees.py` | Kalshi taker fee (integer-cents, clamped) |
| `core/io.py` | Atomic file writes (tmp+rename) |
| `core/risk.py` | Money math + risk caps, one source: entry cap, bankroll-derived (tighten-only) per-order/night caps, station-night key |
| `core/dsm.py` | ASOS Daily Summary Message fetch/parse (IEM AFOS) — the settlement oracle behind the sniper's DSM veto |
| `core/drift.py` | Floor→final drift distribution from the journal — prices floor buy_winners (win prob + EV in alerts) |
| `core/walls.py` | Certainty-wall detection from shadow books (defense vs penny-farm; adversary intel) |
| `core/metar.py` | METAR 6-hourly climate group (1sTTT/2sTTT) fetch/parse — tenths-°C settlement precision |
| `core/take_queue.py` | Staged one-tap approvals: snipers enqueue alerted take.py commands (fcntl-locked, notional-clamped, TTL) |
| `cli_sniper.py` | Race the NWS CLI climate report to its own repricing (cron */2) |
| `metar_sniper.py` | Race the METAR 6-hourly extremes — the pre-CLI leak (cron */5, synoptic windows) |
| `take_approver.py` | One-tap Discord approval → take.py subprocess (cron */1; ✅ allow-list, IOC only, live-book re-check) |
| `dead_bracket_sweeper.py` | Obs-killed brackets still holding bids, all 40 ladders (cron */15) |
| `peak_monitor.py` | Post-peak lock-in alerts, original 5 cities (cron */10, 13-22 ET) |
| `live_watch.py` | Read-only live-account journal + sell-into-strength alert + low-ladder boundary watch near midnight LST (cron */10) |
| `shadow_logger.py` | Dual-venue L2 depth capture for the Poly gate (cron */30) |
| `backtest_collector.py` | Daily settlement ground truth → backtest/daily_data.jsonl (cron 8:00) |
| `market_timeseries.py` | Intraday orderbook snapshots + shared ticker-date parsing (ad-hoc) |
| `backtest/poly_gate_analyzer.py` | Poly go/no-go verdict from shadow books (ad-hoc) |
| `backtest/sniper_scorecard.py` | Joins sniper journal → Kalshi settlement: does the alert win, by how much (cron Sun 17:45) |
| `backtest/cli_timing.py` | Learns real per-office CLI issuance windows from the journal (ad-hoc) |
| `backtest/alert_decay.py` | Edge half-life + reaction budget (min ≤ cap post-alert) via 1-min candles — the daemon go/no-go evidence (ad-hoc) |
| `scripts/take.py` | The ONLY order-placing entry point — human-run; alerts print the exact command |
| `scripts/position_brief.py` | One-shot evidence pack for a ticker: journal prints, live obs, DSM, book, house-rules checklist — paste-ready for a Claude chat (ad-hoc, read-only) |
| `scripts/audit_coverage.py` | Series-drift / parse-health / office-silence self-audit (cron Sun 17:30) |
| `scripts/export_public_stats.py` | Sanitized public snapshot for ethansam.io (cron */30; whitelist + secret-assertions) |
| `weekly_digest.py` | Live summary + dead-bracket base rate + scorecard line (cron Sun 18:00) |
| `watchdog.py` | Heartbeat staleness checks + catch-up spawns (systemd */15) |
| `dashboard_server.py` | Read-only localhost dashboard: health, live account, prices, temps, radar, wall watch |
| `kalshi_client.py` | Kalshi API client — RSA-PSS auth, V2 order placement, checked reads |
| `config.py` | Original 5-city station configs + API client tuning (NOT the ladder registry) |
| `.env` | API credentials (NEVER commit to git) |

## 7. API REFERENCE

### Kalshi (authenticated)
- **Base:** `https://api.elections.kalshi.com/trade-api/v2`
- **Auth:** RSA-PSS signature (key in `.env`)
- **Rate limit:** 10 req/sec, 0.1s min between requests
- **Key endpoints:** `/markets`, `/portfolio/positions`, `/portfolio/balance`,
  `/portfolio/orders` (reads only — order create/cancel moved to
  `/portfolio/events/orders`, V2 single-book schema; the V1 POST/DELETE 410
  since 2026-07)
- **Checked reads:** `get_markets_checked()` / `get_balance_checked()`
  distinguish degraded reads from real empties — `_req_safe` swallows all
  errors into `{}`. Never journal or mark-seen off an unchecked read.
- **Auth failures raise** (`KalshiAuthError`, no retry, not swallowed by
  `_req_safe`): a silent `{}` from a 401 read as "no positions / $0" for an
  evening (2026-07-09). Demo is opt-in only (`demo_mode=True` or
  `KALSHI_DEMO_MODE=true`) — a bare `KalshiClient()` used to mean demo-api,
  whose books are furniture that does not match the live exchange.
- **Order truth is fills, not the instant status:** place_order can report
  `resting` for an IOC that filled nothing (2026-07-10). take.py now reports
  FILLED n/count @ avg from the fills feed 1.5s later.

### IEM (free, no auth, AGGRESSIVELY rate-limited — expect bursts of 429-class refusals)
- **AFOS text archive:** `mesonet.agron.iastate.edu/cgi-bin/afos/retrieve.py?pil={PIL}&fmt=text&limit=N`
  (DSM{awips}, CLI{awips}, historical products — the settlement-forensics feed)
- **Daily summaries:** `/api/1/daily.json` · **hourly ASOS:** `/cgi-bin/request/asos.py`
- One request per station per run, generous timeouts, always fail open on refusal
- Shared AFOS getter (`core/dsm.py: afos_text`) retries a 429 once after a
  short backoff; peak_monitor's 5-city burst does the same (2026-07-16:
  CHI/MIA/LAX/DEN refused serially inside one second)

### NWS (free, no auth, rate-limited)
- **CLI products:** `api.weather.gov/products/types/CLI/locations/{WFO}`
  (per-office listing; the global listing is 2MB/unfilterable)
- **Current obs:** `api.weather.gov/stations/{station}/observations/latest`
- **Settlement page:** `forecast.weather.gov/product.php?site={WFO}&product=CLI&issuedby={AWIPS}`
- **User-Agent required**

## 8. PRINCIPLES

1. **Settlement beats forecasting.** The system that knew tomorrow's temperature
   distribution lost money; the system that reads today's settlement document
   first makes it. Optimize latency-to-truth, not model skill.
2. **Measure, then decide.** Every finding is journaled uncensored; the
   scorecard judges history by the same rules as live code (bug-era rows
   excluded by recomputation, not hand-editing). Gates are pre-registered.
3. **Alert-only automation.** The permission design, the classifier, and the
   ops model all assume no automated order ever fires. Keep it that way.
4. **State files have one writer.** VPS owns the journals; the Mac reads.
5. **Fail closed on state, fail open on filters.** A degraded read never
   journals or marks-seen (retry next cron; the audit says "COULD NOT CHECK",
   never silence). But a safety filter that only REMOVES suggestions (the DSM
   veto) passes through unchecked on feed failure — a human verifies every
   alert anyway, and a filter that can kill the money path is worse than none.
6. **Deletion is a feature.** The KDE stack was 21k LOC of measured -EV;
   deleting it outright beat every "gate it / keep it for reference" option.
   When evidence kills a subsystem, remove it the same week — dead code rots
   into false context for every future session. Never rebuild from nostalgia.
7. **The journal outranks memory.** Uncensored journals + pre-registered gates
   exist because narrative recall flatters itself. When a claim matters, rerun
   it against the journal/archive (the 85/85 DSM study settled what opinion
   couldn't). One data point is an anecdote; an archive is an answer.
8. **Simplicity is a risk control.** Prefer the cron one-shot over the
   resident daemon, the flat file over the database, stdlib urllib over a new
   dependency — every moving part on a live money box is something the
   watchdog must now watch. Complexity gets added only when a measured gate
   (e.g. alert_decay) demands it.
