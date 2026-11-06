# Smart trading agent: build list

Source: the design pastes from the user (Claude as orchestrator, Python as math and guardrails),
checked against the existing code in this folder. Order is the build order.
Every playbook is validated in `backtest.py` before it is enabled live.

## Decisions needed
- [ ] Global open-risk cap value (pastes say 2%; proposed 40% of balance while growing from $100)
- [ ] Economic-calendar data source for the news blackout (nothing in the repo yet)
- [ ] Keep the 60% daily loss limit (pastes say 3%; 3% would lock the bot after one normal stop-out)

## Phase 1: Safety and guardrails (`guardrails.py`, `prompts/system_prompt.md`)
- [x] 1. Global open-risk cap across all positions (`TRADING_MAX_TOTAL_RISK_PCT`, default 40)
- [x] 1c. Symbol order shuffled every scan (found in the first demo scan: the 3-opens-per-scan cap went to whichever symbols came first, so forex filled the slots and 5 approved synthetics were blocked at the same confidence). The order used is stored in `trade_runs.symbols`
  - [ ] Still true: all four open positions were sells and two are short the euro, so the 100 trades will be less independent than 100. Consider a cap on same-direction exposure across asset classes
- [x] 1e. **Position sizing was wrong for some synthetics (found 2026-10-27 after a Volatility 75 trade sized as 2% risk was floating -$1,358).** The bot sized lots from the tick size/value each symbol reports, which are off by 10x or more for some: at a 2% target ($200), a stop-out would have cost Volatility 75 $1,999.80 (10x), Volatility 100 $500 (2.5x), Volatility 50 $20 (0.1x), Crash 1000 $8.70. Now `SymbolInfo.measured_pip_value` is measured with MT5's own `order_calc_profit` and used for sizing, the minimum-lot check and the total-risk cap. Checked on the live terminal: every symbol sizes to $186-$200 for a $200 target; Volatility 100 is refused because its minimum lot can't fit a 2% risk
  - The minimum-lot check now runs on the FINAL risk (after the caps, before it used what the model asked for) and refuses when the smallest lot would risk more than 1.5x the allowed amount (was 3x)
  - Trades opened before this fix were sized wrongly on those symbols: Volatility 50 trade (risked ~$59 instead of $500), the open Volatility 75 trade (~$1,980 at its stop instead of ~$200). Results from them are in the database but their sizes are not what they claim
  - [x] The static minimum-stop table (`_SYNTHETIC_MIN_SL_PIPS`) was wrong by orders of magnitude against real pip sizes and volatility (0.00x of H1 ATR for Boom/Crash 1000, 54x for Volatility 100 (1s)). Removed. Replaced by a rule for every symbol: the stop must be at least `TRADING_MIN_STOP_ATR` (default 1.0) x the symbol's H1 ATR, measured live at decision time (`h1_atr`), 2% slack for pip rounding; blocked if no ATR can be measured; modifies are exempt. Also removed the stale 'typical ATR' table from the prompt. Checked on the live terminal: all 28 symbols are tradable at a 1x ATR stop with 2% risk on $10,000
  - [ ] The early closed-trade statistics are biased toward losses: stopped-out trades close first while winners stay open until their target. Judge results by cohort once most of it has closed, or include open positions marked to market in R
- [x] 1d. Limits are settings, and the prompt follows them. `TRADING_MAX_POSITIONS`, `TRADING_MAX_NEW_OPENS_PER_SCAN`, `TRADING_MAX_SAME_DIRECTION` (new, enforced in code across all symbols), `TRADING_MAX_SYNTHETIC_POSITIONS` (prompt only); the numbers in the prompt are `{{TOKENS}}` filled from the settings (`prompt.py`), so code and prompt can't disagree. The old hard cap of 5 is now a sanity ceiling of 30
  - **TEST PROFILE active since 2026-10-27, widened 2026-11-01** to reach ~100 closed trades faster: now 28 positions (one per watchlist symbol), 10 opens per scan, 14 per direction, 17 synthetics, 1% risk per trade (28 x 1% = 28%, inside the 40% total cap; the forex correlated-direction cap of 2 is unchanged). Before 11-01 it was 12 positions, 6 per scan, 7 per direction, 8 synthetics, 2% risk. **Revert to production before real money:** `TRADING_MAX_POSITIONS=5`, `TRADING_MAX_RISK_PCT=5.0`, `TRADING_MAX_NEW_OPENS_PER_SCAN=3`, `TRADING_MAX_SAME_DIRECTION=3`, `TRADING_MAX_SYNTHETIC_POSITIONS=3` (also written as a comment in `.env`)
  - [ ] Regimes to keep apart when judging the 100 trades: before 2026-10-27 15:30 (5 positions, 5% risk, stale-price stops on the first 5 trades) and after (the fixed, test-profile regime). A `--since` filter on `outcomes` would make that one command
- [x] 1b. One open position per symbol (found during the first demo scan: nothing stopped stacking trades on one symbol)
- [x] 2. Make the prompt and code agree
  - [x] SL >= 1x ATR, every symbol (code-enforced since 2026-10-27, see 1e), measured on the timeframe the stop is anchored to: `submit_trade` has a required `stop_timeframe` (H1/H4/D1) for opens, the floor uses that timeframe's live ATR (H4 is about 2x the H1 ATR, D1 about 5x on every symbol checked), and the declared timeframe must be one the model actually looked at in the session (`evidence.timeframes_used`). Stored per trade (`stop_timeframe`) and shown in `outcomes`
  - [ ] Known limit: `stop_timeframe` is self-reported. The code can't tell which timeframe the model's reasoning really rests on, so it could declare H1 for a D1-based trade. Review after ~100 trades: do trades anchored to slower timeframes do better or worse (`python -m trading outcomes`, "By stop_timeframe"), and does H1 get declared suspiciously often?
  - [ ] D1 must confirm direction (still prompt-only; needs the geometry engine, now stated honestly in the prompt)
  - [x] Maximum 3 new opens per scan (enforced in code)
  - [x] Reconcile the 5-position code cap with "30" in the prompt
- [x] 3. Time and news circuit breaker, enforced in Python before Claude is called (`breakers.py`)
  - [x] High-risk hours and rollovers (`TRADING_BLACKOUT_UTC`, default 21:00-22:00 UTC, forex and metals only)
  - [x] Broker latency spike (`TRADING_MAX_LATENCY_MS`, default 1000)
  - [ ] Economic-calendar blackout for forex (replaces the "Iota" playbook; stand-aside only, no news fading). Waiting on a data-source decision
- [x] 4. Drawdown lockout: suspend Claude calls for 24 hours (`TRADING_LOCKOUT_HOURS`, file `state/LOCKOUT.<mode>`; keeps the 60% limit)
  - [ ] Send the alert somewhere you will see it (it only logs at CRITICAL for now). Needs a channel decision: email, Telegram, etc.
  - [ ] Verify `get_daily_pnl()` and `ping_ms()` against a live MT5 terminal (written from the MT5 API docs, not run against MT5)

## Phase 2: Resilience and state (`broker/metatrader.py`, `audit.py`)
- [x] 5. MT5 reconnect with exponential backoff (`MT5Broker.ensure_connected`, checked before every broker call; waits 1, 2, 4, 8, 16, 30, 30, 30 s, then a 2 minute cool-down; a scan is skipped if the broker stays unreachable). Orders are never retried automatically, since one in flight when the link dropped may have gone through
  - [ ] Never exercised against a real dropped link (tested with a fake terminal). To try it: pull the network cable or disable the adapter for a minute during a scan and read the log
- [x] 6. Crash recovery (`TradingRunner.adopt_orphans`, runs at the start of every live scan and in `python -m trading outcomes`): broker entry deals carrying the bot's magic number (20260001) that have no 'open' row are added as setup "adopted", then `reconcile_outcomes()` fills their result. Counts only trades opened after the first time it ran (stored in the `meta` table as `adopt_since`): the demo account holds 27 older bot trades from Oct 19-21 with no ticket in the database, and adopting them would mix the old, looser regime into the results. Skips entries under 10 minutes old so it can't race a running scan, ignores trades placed by hand. Verified on the real terminal that the broker's position id equals the stored order ticket
  - [ ] Never seen an actual orphan: tested with fakes, and on the real terminal only that the smoke-test trade is recognised as known. To rehearse: stop the loop, delete that trade's row from `trade_decisions`, run `python -m trading outcomes`, and check it is adopted
- [ ] 7. `bot_state.db`: ticket, entry, SL and playbook for every trade

## Phase 3: Cost and orchestration (`loop.py`)
- [ ] 8. Candle-hash skip: no Claude call when a symbol's candle is unchanged
- [ ] 9. One batched watchlist call per candle close, with PASS for no-edge assets

## Phase 4: Geometry engine, pure math, no indicators (new module)
- [x] 10. Swing detector, equal highs and lows (multiple touches within a tolerance), regime classifier (trend or range), BOS and CHoCH (`geometry.py`, no lookahead, tolerances in ATR units)
- [x] 11. Sweep-and-reclaim and FVG detection
  - [x] Payload fields: nearest equal high and low (with distance in ATR), recent sweep, FVG boundaries
  - [x] Exposed to the model as the `get_structure` tool; prompt updated to use it
  - [ ] Calibrate on real data: swing_n, equal-level tolerance (0.15 ATR), min gap size (0.2 ATR). On 500 real bars about a third to a half of equal levels get swept, so a sweep alone is common, not a rare signal. Test whether it has any edge in `backtest.py` before building playbooks on it
  - [x] Signal study built (`study.py`, `python -m trading study`): does regime/sweep/gap/break state at time T predict the next N bars? Baseline-adjusted, de-clustered, out-of-sample split, placebo control; tested on a random walk (no false candidates)
  - [x] Ran the study on real H1 history (28 symbols, 20,000 bars each, 255 tests, horizons 4/12/24): **0 candidates.** Rows past |t|>=2/2.5/3 were 14/4/1 against 11.6/3.2/0.7 expected by chance. Sweeps, FVGs, CHOCH and regime have no usable predictive power at these horizons; hit rates sit within 1-2 points of baseline, and the largest effects (0.05-0.1 ATR) are below forex spreads (0.11-0.28 ATR)
  - [x] Gold/silver regime momentum lead re-tested on data the first study never saw (H4 gold 2011-2022 in three eras, H1 gold and silver 2018-mid2023): **not confirmed** under the rule fixed beforehand (both regimes positive with t>=2 at 12 bars in two or more eras: found in none). It did not even appear on H4 in the first study's own period (+0.06 ATR, t 0.27). The lead was noise. Silver H4 history would not load (0 bars)
  - [ ] Only remaining hint, found after the fact so treat as a new hypothesis, not a result: gold's bearish regime tends to continue lower at 12 H4 bars (t 1.9, 1.2, 1.6 in three eras; none past 2, and gold H1 2021-mid2023 shows nothing). Needs a forward test on data that did not shape it, and must beat spread
  - [ ] Study limits to remember: fixed horizons with no stops or targets; engine parameters untuned (on purpose, to avoid overfitting); H4 not run; Delta/Epsilon not testable yet

## Demo run checkpoints
- 2026-11-02: 25 closed, avg final R -0.03 (t -0.13), 36% wins, 10 trades reached +1R. Post-fix trades (with a stop timeframe) about break-even on 17. Dollar P&L -1358 is mostly three Volatility 75 trades, one pre-sizing-fix.
- 2026-11-04 entry-quality look at 28 closed trades (descriptive, nothing applied): bot avg final R -0.14 vs +0.20 for random entries with the same symbol, direction, stop and target (diff -0.34, t -1.51; the random baseline carries the recent drift in the bot's mostly-sell direction, 23 sells of 28). 17 of 28 hit the full stop; 10 of 19 losers never got more than 0.25R in favour. Fixed take-profit at 0.5 to 2R never beat the actual exits (so exits are not the leak). Hints to test forward, not rules: entries against the prior 24h move -0.55R (n 15) vs with it +0.35R (n 13), Welch t 2.0 but about 12 splits were looked at; Boom/Crash 0 of 3 (-1.06R); buys 1 of 5; forex 3 trades +0.86R, metals 7 -0.21R, Volatility indices 15 -0.12R (random by construction).
- [ ] Store the decision time and the quote at decision in the audit (today only the order time is stored, so the 1.6h scan lag and the planned-vs-actual entry cannot be measured).
- [ ] Pre-register forward tests for the next ~70 trades before changing any rule: (a) forex+metals judged separately from synthetics; (b) no entries against the prior 24h move; (c) Boom/Crash halted; (d) limit orders at the model's level instead of market orders at scan end (needs pending-order support).
- 2026-11-04 give-back check on 28 closed trades: of 19 losers, 10 never got past +0.25R, 3 reached 0.25-0.5R, 5 reached 0.5-1R, only 1 reached +1R (6 of 15 trades that reached +0.5R ended as losses; 1 of 10 that reached +1R). Replay on real M15 paths vs the replay baseline (-0.287R): break-even after +0.5R +0.16R, after +0.75R +0.09R, after +1R -0.03R; trailing and half-off rules -0.01 to -0.06R. Not significant, 7 rules tried on 28 trades: hypothesis only. Any between-scan stop management would need a small mechanical position manager (the bot only wakes every ~5.6h).
- [ ] Forward hypothesis (not built): break-even after +0.5R via a no-model position manager running every few minutes; test on the next ~70 trades as its own cohort.
- [ ] Review again at about 50 closed trades (`python -m trading outcomes`): is the R result settling, which setups and symbols carry it, how much of it is the pre-fix trades. Then at about 100: the verdict (average R above 0 with t >= 2).
- [ ] At about 100: run Entry-timing parts B and C (entry hours and entry quality vs the placebo).

## Scan cost (added 2026-11-01)
- [x] Closed-market skip: forex/metals with a quote older than `TRADING_MARKET_STALE_MIN` (30 min) than the freshest quote on the list are skipped without a model call (weekends: 11 of 28 symbols). Checked live on a Sunday.
- [x] Usage-limit stop: the first "You've hit your limit" error ends the scan (decisions already made still execute) instead of 28 failing calls; the summary line says STOPPED EARLY. The shell loop still sleeps its fixed 4 hours, so a limit that resets later costs one failed call per run.
- [ ] Possible: sleep until the reset time named in the error, and an alert (Telegram/email) when a scan stops early or the lockout trips.

## Entry timing: when do sweeps happen, and is there a better time to enter? (added 2026-10-28, not started)
Idea from the trading clips: look for the best moment to enter, e.g. around liquidity sweeps. What is already known: the signal study tested sweeps as a
signal (28 symbols, 255 tests) and found **no edge**, so "enter on a sweep" is not assumed to work. What is new here is *when* things happen, and the
quality of the bot's own entries. Each piece is a study with its rules written in PLAYBOOKS.md before any result, as in Phases 1-3.
- [x] **A. Sweep timing profile.** Run 2026-10-28 (`python -m trading timing`): 0 candidates in 16 real cells, controls and placebo clean; no slot where sweeps reverse (forex London leans the other way, t -2.91, below the registered hint bar). Zeta stays on hold. Details in PLAYBOOKS.md. When do sweeps of equal highs/lows happen: by hour of day and session (Asian/London/New York), by symbol group,
  and how often price continues vs reverses afterward in each slot. A sweep that is common in one session and flat in the others is the only thing worth a rule.
  Use the existing `geometry.find_levels`; the study already counts sweeps, so this adds the time-of-day cut. Multiple-testing discount: many hour x symbol cells, so
  state the number of cells first and use the study's baseline adjustment and out-of-sample split.
- [ ] **B. Time-of-day check for the bot's own entries.** After about 100 closed demo trades, split results (final R, worst point) by entry hour/session and compare with the
  placebo's random entry times on the same symbols. Answers "does the model enter at bad times". Needs the demo run to finish first; with 8 trades it says nothing.
- [ ] **C. Entry-quality check.** For the bot's entries, compare adverse movement right after entry (the worst point, already recorded) against random entries with the same stop.
  Tests the clip's claim that "price reverses after you buy" and whether the model's timing adds anything.
- [ ] If A finds a time slot where sweeps reverse reliably, write it up as a pre-registered playbook (Zeta, currently on hold) and run it through the same machine with
  jump-stop fills and a baseline placebo. If not, drop it. Whatever is found goes into the prompt only as a measured fact, never as a belief.

## Study B (candidate): daily time-series momentum, a slow sleeve (added 2026-10-28, rules not written yet)
Source: the "Trend Is Your Friend" paper pasted 2026-10-28 (a literature synthesis). Its solid evidence is not the swing-trendline rule (illustrative, one monthly US index,
benefit mostly from sitting in cash) but **time-series momentum on weeks-to-months horizons** (Moskowitz, Ooi and Pedersen and others): an asset's own past return predicts
its next return. That is a daily-bar idea, a different timescale from everything tested so far (H1/H4 intraday rules, all failed). It would be a separate slow sleeve, not part of the intraday agent.
- [ ] Write the rules in PLAYBOOKS.md for approval before building (same discipline as Phases 1-3). Sketch to start from, not yet fixed:
  - D1 bars, forex and metals, each class a separate cell; Volatility indices as the negative control (random: should fail).
  - Signal: sign of the past N-day return (N tied to the paper's 12-month look-back, about 250 D1 bars; maybe also one shorter look-back, stated in advance), long/short/flat, held for a fixed
    holding period, sized to a fixed volatility target.
  - Test through the existing machine or a small variant: it needs a hold-for-many-bars rule, not a stop/target, so check first whether `playbooks/simulate.py` can express it (time-limit exit, a
    wide stop at several ATR) or whether a returns-based test is cleaner.
  - Costs: spread plus **swap/rollover**, which matters for multi-week holds and is not modelled yet. Check what MT5 reports for swaps before trusting any result.
  - History: D1 goes back years on MT5 (gold to 2011 before); check how many years each symbol really has, and use an out-of-sample split by date (the last 40%), with the same placebo.
  - Few independent bets (9 forex pairs share a dollar factor), so the effective sample is small: report that honestly and discount the t-stats.
- [ ] Open question before any build: is a slow daily sleeve wanted at all, given the intraday agent and the daily-loss lockout? A pass would not feed the current agent.
- Note the paper's own caveats: higher-frequency trend signals add little after costs, the reversal side depends on the market, and its headline benefit is drawdown reduction (being in cash), not extra return.

## Phase 5: Playbooks (one module each, each backtested per asset class)
**Now run in phases, see [PLAYBOOKS.md](PLAYBOOKS.md): rules and gate are fixed before any result exists.**
- [x] Phase 0: the test machine (`trading/playbooks/`: interface, replay simulator with pessimistic fills and costs, placebo, pre-registered gate, lookahead checker, `python -m trading playbooks`). Proven first: finds a planted edge, rejects a random walk and a reversed rule, and costs can eat an edge (230 trading tests pass)
- [x] Phase 1: Alpha (H4 trend pullback) and Beta (H1 London breakout of the Asian range) built, mutation-checked (each rule condition removed in turn makes a test fail), lookahead-clean, and run once on forex and metals: **all four cells FAIL, both shelved.** Alpha forex -0.040 R (t -1.52), Alpha metals +0.102 R (t 1.75, out of sample t 1.37), Beta forex -0.062 R (t -2.96), Beta metals -0.072 R. The placebo loses 0.09 R per trade on H1 forex, which is the spread; anything later must earn that first. Details in PLAYBOOKS.md
  - [x] Alpha's one near miss (metals, +0.102 R) re-tested once on XAUUSD H4 from 2011 to 2021-10-25, data the rules had never seen, registered in advance: +0.010 R, t 0.17, out of sample -0.140 R. **FAIL. The near miss was noise; Alpha is closed for metals too
- [x] Phase 3: Delta (Boom/Crash spike fade, M5) and Epsilon (Step z-score fade, M15). Run 2026-10-28. Epsilon FAIL. Delta passed the gate (+0.067 R, t 4.9) but that was a fill artifact: with jump-stop fills it is -0.095 R, and it equals a plain trade-with-the-drift placebo. Both shelved, no survivors. Added `--jump-stops` to the playbooks command
- [x] Edited the two unsupported statements (done 2026-10-28; Boom/Crash and Step sections now say no tested edge)  [was: decide whether to edit the two unsupported statements in prompts/system_prompt.md (Boom/Crash "spike fade", Step "mean-reversion works well")]
- [x] Phase 2: Gamma (range-edge fade in a RANGE regime) and Theta (z-score fade), H4, symmetric 2 ATR stop and target, 12-bar limit, forex and metals plus Volatility indices as a negative control. Built and run once on 2026-10-28: all six cells FAIL (forex about zero before costs, metals clearly negative, Volatility control failed as expected). Both shelved, results in PLAYBOOKS.md
- [x] Phase 3 done (see above). [ ] Phase 4: survivors only (none so far)

- [ ] 12. Alpha: structural pullback (forex, volatility indices)
- [ ] 13. Beta: session breakout (forex only)
- [ ] 14. Gamma: boundary mean reversion (ranging synthetics, flat forex)
- [ ] 15. Delta: spike capture (Boom/Crash)
- [ ] 16. Epsilon: step channel (Step indices)
- [ ] 17. Zeta: liquidity sweep reversal. **On hold: the signal study found no edge** (forex sweep:bullish +0.035 ATR, t 0.75; sweep:bearish -0.001, t -0.01)
- [ ] 18. Eta: FVG fill. **On hold: the signal study found no edge** (forex fvg:bullish -0.054 ATR, t -1.13)
- [ ] 19. Registry and routing
  - [ ] Tier 1: Python pre-filters playbooks by asset class
  - [ ] Tier 2: Claude chooses among the remaining playbooks
- [ ] 22. Theta: Z-score mean reversion, built with Gamma as one module with two modes
- [ ] 24. Playbook-aware `submit_trade` schema: add `playbook` and `playbooks_considered` (`decision.py`)

## Phase 6: Audit and rollout
- [ ] 20. SHA-256 hash chaining in `audit.py`
- [ ] 21. Paper trade for 2 to 4 weeks, review the logs daily, then go live
- [x] Outcome tracking: each open now records its broker ticket; `reconcile_outcomes()` fills profit, close price and win/loss at the start of every scan from the MT5 deal history; `python -m trading outcomes` shows P&L by setup and symbol
  - [x] Verified `get_closed_trade()` on the first real closed demo trade (XAGUSD sell, stopped out at -$174.00; equals 0.29 x 0.12 lots x 5000 oz exactly, and the balance fell from 10000.00 to 9826.00)
  - [x] Best and worst point per trade (`mfe_r`, `mae_r`, `final_r`, in multiples of initial risk), measured from M1 candles over the trade's lifetime at each reconcile and shown in `python -m trading outcomes`. First measurement, XAGUSD: best +0.66R, worst -0.24R, ended -0.27R (stopped out after the model tightened the stop to a level still above entry)
  - [x] **Bug found by that measurement and fixed:** stop and target were priced from the quote at analysis time, but orders go out up to ~100 minutes later, so the first 4 scan-1 trades and Crash 500 carry stops at the wrong distance from the real fill (XAGUSD 108 pips vs 80 intended, EURJPY 63 vs 50, Volatility 50 24,285 vs 20,500, Crash 500 3,383 vs 4,000; EURUSD was correct). `_open_trade` now prices off the quote at execution. The early trades were not sized at exactly 5% risk
  - [ ] Judge the 100-trade verdict in R (`final_r_t_stat`), not dollars, because the first trades were not uniformly sized
  - [ ] After ~30-50 closed trades: replay each trade's candles under candidate exit rules (breakeven at +1R, trailing) and compare with what the model did
  - Opens made before this change have no ticket and stay "no_result_yet" (the Oct 19-21 history)
- [ ] Measure Claude's confidence against actual outcomes (now possible from the audit database) before relying on risk scaling by confidence

## Dropped
- Deriv WebSocket and Pandas ingestion: the bot already gets candles through MT5 in plain Python
- News-fading as a trading strategy: too risky on a small account; only the blackout is kept

## 2026-11-04 changes after the 28-trade review (cohort boundary: trades opened from here on)
- TRADING_MIN_STOP_ATR 1.0 -> 2.0 (replay of 28 closed trades: ~+0.4R, not significant; chosen because stops were swept, not proven).
- TRADING_MAX_ENTRY_DRIFT=0.3: skip an order if price moved more than 0.3x the stop distance since the model decided (scan lag up to ~2h). Skips show as blocked "stale_entry" with a note in the audit row. Judge forward: compare trades opened before/after this date separately; do not pool.
- Not done: limit orders at the model level, no-entry-against-24h-move rule, break-even manager (all hypotheses only).

## Cohort rules (written 2026-11-04 before any result; option C chosen)
- Boom/Crash removed from TRADING_SYMBOLS (3 trades, -1.06R, no tested edge). Volatility and Step stay as a process check only; they are not part of the verdict.
- Primary cohort: forex + metals, trades opened from the restart date (use --since). View: `python -m trading outcomes --cohort forex-metals --since YYYY-MM-DD`. Pre-change trades are not pooled.
- Look only at n>=30 forex/metals closed trades. Futility stop at n>=30: avg R < -0.25 and t < -1 -> stop and redesign, do not keep going.
- Verdict at n>=60: edge if avg R > 0 and t >= 2 (60 trades only detects a true +0.3R; a smaller edge needs ~150, so no result at 60 means "unproven", not "none").
- No parameter changes inside the cohort; one change = a new cohort with a new date.

## Cohort 2 starts 2026-11-06 (bot restarted after a ~2 day gap with no scans; settings: min stop 2x ATR, stale-entry guard 0.3, Boom/Crash removed). Adopted (not bot-opened) positions are excluded from stats.
- Pre-change record at restart: 33 measured bot trades, avg -0.17R, t -0.83.
- Check with: `python -m trading outcomes --cohort forex-metals --since 2026-11-06`; also `--cohort synthetic --since 2026-11-06`.

## Scope: limit-order entries (not built; start only if cohort 2 forex/metals avg R is still negative at n>=30)
Idea: the model names the price it wants; the bot places a pending buy/sell limit there instead of a market order up to ~2h later. Better price or no trade.
Current code: `broker/metatrader.py` `place_order` sends only market deals (TRADE_ACTION_DEAL); `_open_trade` in `runner.py` prices SL/TP off the live quote.

Build list
1. Dry-run check first (no order placed): `order_check` of BUY_LIMIT/SELL_LIMIT with an expiry on one forex, one metal and one Volatility symbol on the Deriv demo. If the server refuses pending orders on a class, that class stays on market orders.
2. Decision schema: optional `entry_price`; SL/TP pips are measured from it. Guardrails: must be on the correct side of the market (buy below ask, sell above bid), no further than ~1.5x ATR away, spread and stop-ATR checks measured from the limit price.
3. Broker: `place_limit_order` (type, price, SL, TP, expiry ~ one scan interval + margin, same magic number), `cancel_order`, `get_pending_orders`.
4. Caps must count pending orders: max positions, one per symbol, same-direction, 40% open-risk cap, per-scan open cap. Otherwise a fill burst breaks the caps.
5. Audit/outcomes: row state pending -> filled (position ticket via deal lookup) / expired (outcome n/a, kept). `adopt_orphans` and crash recovery must not mis-adopt pending or filled-later orders. Fill rate becomes a reported number.
6. Tests with a fake mt5: side/distance guardrails, cap counting, expiry, fill matching, no double fills after a restart.
Estimate: 1-2 days with tests.

Pre-registered evaluation (written now): new cohort 3, same symbols/settings as cohort 2 except entry type. Report fill rate, avg R of filled trades, and avg R per SIGNAL counting unfilled as 0R. Limit orders only count as better if per-signal avg R beats cohort 2 market entries by >=0.1R with the filled-trade R not worse. Warn: limit fills are adverse-selected (they fill most when price is moving against the idea), so avg R of filled trades alone proves nothing; also track the R of the setups that never filled.

## Scope: structured chart read (not built; independent of the limit-order work)
Why: the model already reads charts discretionarily (liquidity sweeps, trend, ranges) but only the final trade is scored, ~1 per 4 symbols. Prose reasons can't be checked against price. If the model writes its map as data on EVERY symbol it analyses (trade or not), each scan yields ~20 scoreable reads instead of ~5 trades, so we learn which of its reads are real in days, not months.

Build list
1. Add a `submit_chart_read` call (separate from `submit_trade`, required before the decision) with fields: bias (up/down/range), timeframe read, `levels` [{type: liquidity_high|liquidity_low|fvg|order_block|major_level, price, side, note}], `sweep` (which level, up/down, when), `entry_idea` {price, invalidation, target} or none, confidence. Store as JSON in a new `chart_reads` table keyed by run_id + symbol + created_at (additive migration, like the existing columns).
2. Prompt: ask for the read from what it actually sees, no required strategy; "nothing clear" is a valid read.
3. Scoring job `python -m trading score-reads`, run on reads older than N hours using later candles (no model call). Pre-defined checks, written now:
   - Level touched within 24h (yes/no) and distance travelled before touch, in ATR.
   - Level held vs broken on first touch (held = closes back on the original side within 3 bars).
   - Claimed sweep: did price actually trade through the level and close back inside within 3 bars of the claimed time (verifies the claim itself, not just what came after).
   - Bias: did the next 12 H1 bars close in the stated direction, vs the base rate of that symbol (drift-adjusted).
   - Entry idea: reached? If reached, did target hit before invalidation (R result, spread-adjusted)?
4. Report by read type, class and timeframe, with n, hit rate vs a placebo (same checks on random levels/prices on the same symbol) and the usual t >= 2 / out-of-sample-last-40% discipline. A read type only counts as skill if it beats the placebo.
5. Tests: scoring functions on hand-built candle fixtures (touch, hold, break, false sweep claim), prompt/schema round-trip, migration on an old database.
Optional extra (later, only if the data says the model's reads are good but its numeric view is the limit): chart images in the prompt. Costs tokens and the model reads numbers well already, so not first.
Use: the scores decide which setups the model is allowed to trade (a read type with no edge vs placebo is removed from the prompt), and feed entries (limit orders at the model's own marked level).
Estimate: 1-2 days. Cost: one extra tool call per symbol per scan (small).
