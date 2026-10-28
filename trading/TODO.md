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
  - **TEST PROFILE active since 2026-10-27** to reach ~100 closed trades faster: 12 positions, 6 opens per scan, 7 per direction, 8 synthetics, 2% risk per trade (12 x 2% = 24%, inside the 40% total cap). **Revert to production before real money:** `TRADING_MAX_POSITIONS=5`, `TRADING_MAX_RISK_PCT=5.0`, `TRADING_MAX_NEW_OPENS_PER_SCAN=3`, `TRADING_MAX_SAME_DIRECTION=3`, `TRADING_MAX_SYNTHETIC_POSITIONS=3` (also written as a comment in `.env`)
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

## Phase 5: Playbooks (one module each, each backtested per asset class)
**Now run in phases, see [PLAYBOOKS.md](PLAYBOOKS.md): rules and gate are fixed before any result exists.**
- [x] Phase 0: the test machine (`trading/playbooks/`: interface, replay simulator with pessimistic fills and costs, placebo, pre-registered gate, lookahead checker, `python -m trading playbooks`). Proven first: finds a planted edge, rejects a random walk and a reversed rule, and costs can eat an edge (230 trading tests pass)
- [x] Phase 1: Alpha (H4 trend pullback) and Beta (H1 London breakout of the Asian range) built, mutation-checked (each rule condition removed in turn makes a test fail), lookahead-clean, and run once on forex and metals: **all four cells FAIL, both shelved.** Alpha forex -0.040 R (t -1.52), Alpha metals +0.102 R (t 1.75, out of sample t 1.37), Beta forex -0.062 R (t -2.96), Beta metals -0.072 R. The placebo loses 0.09 R per trade on H1 forex, which is the spread; anything later must earn that first. Details in PLAYBOOKS.md
  - [x] Alpha's one near miss (metals, +0.102 R) re-tested once on XAUUSD H4 from 2011 to 2021-10-25, data the rules had never seen, registered in advance: +0.010 R, t 0.17, out of sample -0.140 R. **FAIL. The near miss was noise; Alpha is closed for metals too
- [x] Phase 3: Delta (Boom/Crash spike fade, M5) and Epsilon (Step z-score fade, M15). Run 2026-10-28. Epsilon FAIL. Delta passed the gate (+0.067 R, t 4.9) but that was a fill artifact: with jump-stop fills it is -0.095 R, and it equals a plain trade-with-the-drift placebo. Both shelved, no survivors. Added `--jump-stops` to the playbooks command
- [x] Edited the two unsupported statements (done 2026-10-28; Boom/Crash and Step sections now say no tested edge)  [was: decide whether to edit the two unsupported statements in prompts/system_prompt.md (Boom/Crash "spike fade", Step "mean-reversion works well")]
- [x] Phase 2: Gamma (range-edge fade in a RANGE regime) and Theta (z-score fade), H4, symmetric 2 ATR stop and target, 12-bar limit, forex and metals plus Volatility indices as a negative control. Built and run once on 2026-10-28: all six cells FAIL (forex about zero before costs, metals clearly negative, Volatility control failed as expected). Both shelved, results in PLAYBOOKS.md
- [ ] Phase 3: Delta and Epsilon (synthetics, expected null). Phase 4: survivors only

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
