"""One scan over all configured symbols.

For each symbol:
  1. Run the MarketAgent reasoning loop → TradeResult
  2. Apply guardrails (risk rules)
  3. Execute the trade (paper engine or live MT5)
  4. Log the decision to the audit DB

The scan stops immediately if the kill-switch file exists.
"""
from __future__ import annotations

import logging
import random
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from agentcore.sdk_backend import build_client

from .audit import TradeAudit
from .breakers import Lockout, closed_markets, halt_reason, parse_blackouts
from .broker.metatrader import MT5Broker, Position, SymbolInfo
from .config import Settings
from .decision import CLOSE, MODIFY, OPEN, WAIT, TradeDecision
from .guardrails import apply_guardrails, atr_for, open_risk_pct
from .loop import AgentError, MarketAgent, TradeResult, UsageLimitError
from .paper import PaperEngine
from .prompt import render_prompt
from .tools import TradingToolbox

log = logging.getLogger(__name__)


@dataclass
class ScanSummary:
    run_id: str = ""
    symbols_scanned: int = 0
    decided: int = 0
    errors: int = 0
    actions: Counter = field(default_factory=Counter)
    blocked: Counter = field(default_factory=Counter)
    mode: str = "paper"
    halted: str = ""             # set when the whole scan was skipped by a lockout
    usage_limit: str = ""        # set when the scan stopped early because the model's usage limit ran out

    def line(self) -> str:
        if self.halted:
            return f"trading scan {self.run_id} [{self.mode}]: HALTED, no model calls: {self.halted}"
        acts = ", ".join(f"{k}={v}" for k, v in sorted(self.actions.items())) or "none"
        blocked = ", ".join(f"{k}={v}" for k, v in sorted(self.blocked.items())) or "none"
        line = (f"trading scan {self.run_id} [{self.mode}]: "
                f"symbols={self.symbols_scanned} decided={self.decided} errors={self.errors} "
                f"actions[{acts}] blocked[{blocked}]")
        return line + (f" STOPPED EARLY, model usage limit: {self.usage_limit}" if self.usage_limit else "")


class TradingRunner:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self._broker = MT5Broker()
        self._paper = PaperEngine(settings.paper_db) if settings.mode == "paper" else None
        self._audit = TradeAudit(settings.state_db)
        self._lockout = Lockout(settings.lockout_file)
        self._agent: Optional[MarketAgent] = None
        self._risk_manager: Optional[Any] = None

    # ---- lifecycle ---------------------------------------------------------------

    def connect(self, retries: int = 5, delay: float = 4.0) -> None:
        self.connect_broker(retries, delay)
        prompt = render_prompt(self.s.prompt_file.read_text(encoding="utf-8"), self.s)
        client = build_client(self.s)
        self._build_agent(prompt, client)

    def connect_broker(self, retries: int = 5, delay: float = 4.0) -> None:
        import time
        from .broker.metatrader import MT5Error
        last_exc: Optional[Exception] = None
        for attempt in range(1, retries + 1):
            try:
                self._broker.connect(self.s.mt5_login, self.s.mt5_password, self.s.mt5_server)
                last_exc = None
                break
            except MT5Error as exc:
                last_exc = exc
                if attempt < retries:
                    log.warning("MT5 connect attempt %d/%d failed (%s); retrying in %.0fs", attempt, retries, exc, delay)
                    time.sleep(delay)
        if last_exc:
            raise last_exc

    def _build_agent(self, prompt: str, client: Any) -> None:
        def _history(limit: int = 20) -> list[dict]:
            if self._paper:
                return self._paper.get_history(limit)
            return self._audit.recent(limit, mode="live")

        toolbox = TradingToolbox(self._broker, history_fn=_history)
        self._agent = MarketAgent(
            client=client,
            model=self.s.model,
            system_prompt=prompt,
            toolbox=toolbox,
            max_turns=self.s.max_agent_turns,
            max_tokens=self.s.max_tokens,
        )
        # RM disabled: consistently hits rate limit after 28-symbol scans, vetoing all trades.
        # Guardrails in apply_guardrails() enforce all hard risk rules; RM is redundant here.
        self._risk_manager = None

    def disconnect(self) -> None:
        self._broker.disconnect()

    # ---- crash recovery ----------------------------------------------------------

    def adopt_orphans(self, lookback_days: int = 30, grace_minutes: float = 10.0) -> int:
        """Record positions the bot opened that the database has no row for.

        An order can reach the broker and the process die before the row is written. The
        deal history still has it, tagged with the bot's magic number, so it is added here
        (setup "adopted") and reconcile_outcomes() then fills in its result like any other.
        Positions opened by hand carry no magic number and are left alone.

        Two limits keep it from adopting the wrong things:
          - Entries newer than `grace_minutes` are skipped: a scan running at the same moment
            may be about to write that row itself, and a crash orphan can wait for the next pass.
          - Only entries opened after the first time this ran are considered. Earlier trades
            were made before tickets were recorded, so their rows have no ticket and would
            all look like orphans, and adopting them would mix an old regime into the results.
        """
        if self.s.mode != "live":
            return 0
        now = datetime.now(timezone.utc)
        anchor = self._audit.get_meta("adopt_since")
        if anchor is None:
            self._audit.set_meta("adopt_since", now.isoformat())
            log.info("crash recovery starts counting from %s; earlier trades are not adopted", now.isoformat(timespec="seconds"))
            return 0
        since = max(now - timedelta(days=lookback_days), datetime.fromisoformat(anchor))
        settled = now - timedelta(minutes=grace_minutes)
        known = self._audit.known_open_tickets("live")
        orphans = [e for e in self._broker.get_bot_entries(since)
                   if e.position_id not in known and e.opened_at <= settled]
        if not orphans:
            return 0
        held = {p.ticket: p for p in self._broker.get_positions()}
        run_id = self._audit.start_run("live", [])
        for e in orphans:
            still_open = held.get(e.position_id)
            self._audit.record(
                run_id=run_id, mode="live", symbol=e.symbol,
                proposed_action=OPEN, final_action=OPEN, direction=e.direction,
                lots=e.lots, entry_price=e.price,
                sl_price=still_open.sl if still_open else 0.0,
                tp_price=still_open.tp if still_open else 0.0,
                setup="adopted",
                reason="Found in the broker's deal history with no database record "
                       "(the process likely stopped between sending the order and saving it).",
                guardrail_notes=["adopted: no database record"],
                position_ticket=e.position_id,
                executed=[f"adopted {e.direction} {e.lots} lots @ {e.price:.5f} (ticket={e.position_id})"],
                created_at=e.opened_at,
            )
        self._audit.finish_run(run_id, len(orphans), 0, {OPEN: len(orphans)})
        log.warning("adopted %d position(s) the database had no record of: %s", len(orphans),
                    ", ".join(f"{e.symbol} #{e.position_id}" for e in orphans))
        return len(orphans)

    # ---- outcomes ----------------------------------------------------------------

    def reconcile_outcomes(self) -> int:
        """Fill in profit, close price and win/loss for opens whose position has since closed.

        Reads the broker's deal history (or the paper table), so it also catches stops,
        targets and manual closes. Then measures how each newly closed trade moved while it was
        open (see _measure_excursions). Returns how many results were recorded.
        """
        if self.s.mode == "paper":
            assert self._paper is not None
            lookup = self._paper.get_closed_trade
        else:
            lookup = self._broker.get_closed_trade
        recorded = 0
        for row in self._audit.unresolved_opens(self.s.mode):
            closed = lookup(row["position_ticket"])
            if closed is None:
                continue
            self._audit.record_outcome(row["id"], closed.close_price, closed.profit, closed.closed_at)
            recorded += 1
        if recorded:
            log.info("recorded %d trade outcome(s)", recorded)
        self._measure_excursions(lookup)
        return recorded

    def _measure_excursions(self, lookup) -> int:
        """For closed trades not yet measured: the best and worst the price got while the trade was
        open, and where it ended, all in multiples of the initial risk (distance from the actual
        fill to the original stop). Lets exit management be judged on real trades later, for
        example how often a trade that was well ahead ended as a loss.

        A trade that can't be measured (no stop recorded, or no price history for its lifetime)
        is marked tried and left alone. Nothing is attempted while the broker is unreachable.
        """
        pending = self._audit.closed_needing_excursion(self.s.mode)
        if not pending or not self._broker.ensure_connected():
            return 0
        measured = 0
        for row in pending:
            closed = lookup(row["position_ticket"])
            risk = abs(closed.open_price - row["sl_price"]) if closed and closed.open_price and row["sl_price"] else 0.0
            span = None
            if closed and closed.opened_at and risk > 0:
                span = self._broker.get_price_excursion(
                    row["symbol"], row["direction"], closed.open_price, closed.opened_at, closed.closed_at)
            if span is None:
                self._audit.record_excursion(row["id"])
                continue
            sign = 1 if row["direction"] == "buy" else -1
            final = sign * (closed.close_price - closed.open_price)
            self._audit.record_excursion(row["id"], span[0] / risk, span[1] / risk, final / risk)
            measured += 1
        if measured:
            log.info("measured price excursion for %d closed trade(s)", measured)
        return measured

    # ---- main scan ---------------------------------------------------------------

    def _closed_markets(self, symbols: list[str]) -> dict[str, str]:
        """Forex/metals symbols to skip because their market is closed (see breakers.closed_markets)."""
        quote_time = getattr(self._broker, "last_tick_time", None)
        if quote_time is None or self.s.market_stale_min <= 0:
            return {}
        try:
            return closed_markets({sym: quote_time(sym) for sym in symbols}, self.s.market_stale_min)
        except Exception as exc:  # noqa: BLE001 — a failed check must not stop the scan
            log.warning("could not check which markets are closed: %s", exc)
            return {}

    def run(self, symbols: Optional[list[str]] = None) -> ScanSummary:
        assert self._agent is not None, "Call connect() first"
        # The per-scan cap on new opens goes to whichever symbols are reached first, so the order is
        # shuffled each scan: otherwise forex and metals would always fill the slots before the
        # synthetics are looked at. The order used is stored with the run.
        symbols = list(symbols or self.s.symbols)
        random.shuffle(symbols)
        summary = ScanSummary(
            run_id=self._audit.start_run(self.s.mode, symbols),
            mode=self.s.mode,
        )

        # Without the broker the model would see empty data for every symbol and waste the whole scan.
        if not self._broker.ensure_connected():
            summary.halted = "MT5 unreachable after reconnect attempts; scan skipped"
            self._audit.finish_run(summary.run_id, 0, 0, {})
            log.error("ALERT: %s", summary.line())
            return summary

        try:
            self.adopt_orphans()
            self.reconcile_outcomes()
        except Exception as exc:  # noqa: BLE001 — bookkeeping must never stop a scan
            log.warning("could not adopt orphans or reconcile trade outcomes: %s", exc)

        account = self._broker.get_account()
        all_positions = self._positions()
        daily_pnl = self._daily_pnl()

        # ── Drawdown lockout: no model calls at all while it holds ────────────
        now = datetime.now(timezone.utc)
        locked = self._lockout.active(now)
        if locked is None and account and self.s.daily_loss_limit_pct > 0:
            limit = account.balance * self.s.daily_loss_limit_pct / 100.0
            if daily_pnl <= -limit:
                reason = (f"daily P&L {daily_pnl:.2f} reached the -{limit:.2f} limit "
                          f"({self.s.daily_loss_limit_pct}% of balance)")
                locked = (self._lockout.trip(now, self.s.lockout_hours, reason), reason)
                log.critical("ALERT: drawdown lockout tripped: %s. No model calls until %s UTC "
                             "(delete %s to lift it).", reason, f"{locked[0]:%Y-%m-%d %H:%M}",
                             self.s.lockout_file)
        if locked:
            summary.halted = f"drawdown lockout until {locked[0]:%Y-%m-%d %H:%M} UTC: {locked[1]}"
            self._audit.finish_run(summary.run_id, 0, 0, {})
            log.critical("ALERT: %s", summary.line())
            return summary

        ping_ms = self._broker.ping_ms()
        windows = parse_blackouts(self.s.blackout_utc)
        closed = self._closed_markets(symbols)

        # ── Phase 1: Analyst — collect decisions for all symbols ──────────────
        proposals: list[tuple[str, TradeResult, Any, Optional[SymbolInfo]]] = []
        sim_positions = list(all_positions)  # grows with pending opens for count check
        _pending_ticket = 90000
        # Risk already on the table; grows by each open approved earlier in this scan.
        total_open_risk = open_risk_pct(
            all_positions, account.balance if account else 0.0,
            self._broker.get_symbol_info, self.s.max_risk_pct,
        )

        for symbol in symbols:
            if self.s.kill_switch_file.exists():
                log.warning("Kill switch exists; stopping scan at %s", symbol)
                break

            summary.symbols_scanned += 1
            log.info("Scanning %s ...", symbol)

            # Forex and metals with no fresh quote (weekend) can neither be analysed nor traded.
            if symbol in closed:
                log.info("%s: skipped, %s", symbol, closed[symbol])
                self._audit.record(
                    run_id=summary.run_id, mode=self.s.mode, symbol=symbol,
                    proposed_action=WAIT, final_action=WAIT,
                    guardrail_notes=[closed[symbol]],
                )
                summary.blocked["market_closed"] += 1
                continue

            # A halt blocks new opens. With nothing open on the symbol there is nothing
            # to manage, so skip the model call; otherwise it may still close or tighten.
            halted = halt_reason(symbol, datetime.now(timezone.utc), windows, ping_ms, self.s.max_latency_ms)
            if halted and not any(p.symbol == symbol for p in all_positions):
                log.info("%s: skipped, %s", symbol, halted)
                self._audit.record(
                    run_id=summary.run_id, mode=self.s.mode, symbol=symbol,
                    proposed_action=WAIT, final_action=WAIT,
                    guardrail_notes=[f"trading halted: {halted}"],
                )
                summary.blocked["halted"] += 1
                continue

            try:
                result = self._agent.decide(symbol, account, all_positions)
            except AgentError as exc:
                log.error("%s: agent error: %s", symbol, exc)
                summary.errors += 1
                self._audit.record(
                    run_id=summary.run_id, mode=self.s.mode, symbol=symbol,
                    proposed_action=WAIT, final_action=WAIT,
                    error=str(exc)[:500],
                )
                if isinstance(exc, UsageLimitError):
                    # The rest of the symbols would fail the same way, so stop here. Decisions already
                    # made in this scan still go on to execution below.
                    summary.usage_limit = str(exc)[:200]
                    log.error("ALERT: model usage limit reached, stopping the scan after %d symbol(s): %s",
                              summary.symbols_scanned, exc)
                    break
                continue

            symbol_info = self._broker.get_symbol_info(symbol)
            guard = apply_guardrails(
                decision=result.decision,
                account=account,
                positions=sim_positions,
                symbol_info=symbol_info,
                max_risk_pct=self.s.max_risk_pct,
                max_positions=self.s.max_positions,
                daily_loss_limit_pct=self.s.daily_loss_limit_pct,
                spread_max_pips=self.s.spread_max_pips,
                synthetic_spread_max_pips=self.s.synthetic_spread_max_pips,
                confidence_threshold=self.s.confidence_threshold,
                daily_pnl=daily_pnl,
                open_risk_pct=total_open_risk,
                max_total_risk_pct=self.s.max_total_risk_pct,
                opens_this_scan=len(sim_positions) - len(all_positions),
                halted=halted,
                max_new_opens=self.s.max_new_opens_per_scan,
                max_same_direction=self.s.max_same_direction,
                atr_price=(atr_for(self._broker, symbol, result.decision.stop_timeframe or "H1")
                           if result.decision.action == OPEN else None),
                min_stop_atr=self.s.min_stop_atr,
                analysed_timeframes=frozenset(result.evidence.timeframes_used),
            )
            proposals.append((symbol, result, guard, symbol_info))

            if guard.final.action == OPEN and not guard.blocked:
                total_open_risk += guard.final.risk_pct
                sim_positions.append(Position(
                    ticket=_pending_ticket, symbol=symbol,
                    direction=guard.final.direction, lots=0.01,
                    open_price=0.0, sl=0.0, tp=0.0, profit=0.0,
                    open_time=datetime.now(timezone.utc),
                ))
                _pending_ticket += 1

        # ── Phase 2: Risk Manager — review the open proposals ─────────────────
        rm_verdicts: dict[str, Any] = {}
        if self._risk_manager is not None:
            open_proposals = [g.final for _, _, g, _ in proposals
                              if g.final.action == OPEN and not g.blocked]
            if open_proposals:
                rm_verdicts = self._risk_manager.review(open_proposals, account, all_positions)

        # ── Phase 3: Execute ───────────────────────────────────────────────────
        for symbol, result, guard, symbol_info in proposals:
            decision = result.decision
            final = guard.final

            if guard.blocked:
                log.info("%s: blocked → wait: %s", symbol, "; ".join(guard.notes))
                summary.blocked[decision.action] += 1

            if final.action == OPEN and symbol in rm_verdicts:
                verdict = rm_verdicts[symbol]
                modified = verdict.apply(final)
                if modified.action == WAIT:
                    log.info("%s: RM vetoed: %s", symbol, verdict.reason)
                    summary.blocked[OPEN] += 1
                elif modified.risk_pct != final.risk_pct:
                    log.info("%s: RM resized %.1f%% → %.1f%%: %s",
                             symbol, final.risk_pct, modified.risk_pct, verdict.reason)
                final = modified

            executed, error, lots, entry_price, sl_price, tp_price, opened_ticket = \
                self._execute(symbol, final, symbol_info, account)

            self._audit.record(
                run_id=summary.run_id,
                mode=self.s.mode,
                symbol=symbol,
                proposed_action=decision.action,
                final_action=final.action,
                direction=final.direction,
                risk_pct=final.risk_pct,
                sl_pips=final.sl_pips,
                tp_pips=final.tp_pips,
                lots=lots,
                entry_price=entry_price,
                sl_price=sl_price,
                tp_price=tp_price,
                confidence=decision.confidence,
                setup=decision.setup,
                reason=decision.reason,
                evidence=decision.evidence,
                guardrail_notes=guard.notes,
                position_ticket=opened_ticket or final.position_ticket,
                stop_timeframe=final.stop_timeframe,
                executed=executed,
                error=error,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                turns=result.turns,
            )

            summary.decided += 1
            summary.actions[final.action] += 1

            if final.action in (OPEN, CLOSE):
                all_positions = self._positions()

        self._audit.finish_run(summary.run_id, summary.decided, summary.errors, dict(summary.actions))
        log.info(summary.line())
        return summary

    # ---- execution ---------------------------------------------------------------

    def _execute(
        self,
        symbol: str,
        decision: TradeDecision,
        symbol_info: Optional[SymbolInfo],
        account,
    ) -> tuple[list[str], Optional[str], float, float, float, float, Optional[int]]:
        """Execute the decision. Returns (executed_steps, error, lots, entry, sl, tp, opened_ticket)."""
        done: list[str] = []
        lots = sl_price = tp_price = entry_price = 0.0
        opened_ticket: Optional[int] = None

        if decision.action == WAIT:
            return done, None, lots, entry_price, sl_price, tp_price, opened_ticket

        try:
            if decision.action == OPEN:
                lots, entry_price, sl_price, tp_price, opened_ticket = \
                    self._open_trade(symbol, decision, symbol_info, account)
                done.append(f"opened {decision.direction} {lots} lots @ {entry_price:.5f} (ticket={opened_ticket})")

            elif decision.action == CLOSE:
                ticket = decision.position_ticket
                exit_price = self._current_price(symbol, decision.direction)
                if self.s.mode == "paper":
                    assert self._paper is not None
                    pnl = self._paper.close_position(ticket, exit_price)
                    done.append(f"paper closed ticket={ticket} @ {exit_price:.5f} P&L={pnl}")
                else:
                    self._broker.close_position(ticket)
                    done.append(f"live closed ticket={ticket}")

            elif decision.action == MODIFY:
                ticket = decision.position_ticket
                pos = next((p for p in self._positions() if p.ticket == ticket), None)
                if pos and symbol_info:
                    sl_p, tp_p = self._sl_tp_prices(
                        pos.open_price, pos.direction, decision.sl_pips, decision.tp_pips, symbol_info
                    )
                    if self.s.mode == "paper":
                        assert self._paper is not None
                        self._paper.modify_position(ticket, sl_p, tp_p)
                    else:
                        self._broker.modify_position(ticket, sl_p, tp_p)
                    done.append(f"modified ticket={ticket} SL={sl_p:.5f} TP={tp_p:.5f}")

        except Exception as exc:  # noqa: BLE001
            log.error("%s: execution failed: %s", symbol, exc)
            return done, str(exc)[:500], lots, entry_price, sl_price, tp_price, opened_ticket

        return done, None, lots, entry_price, sl_price, tp_price, opened_ticket

    def _open_trade(
        self,
        symbol: str,
        decision: TradeDecision,
        symbol_info: Optional[SymbolInfo],
        account,
    ) -> tuple[float, float, float, float, int]:
        # The decision was made when this symbol was analysed, which can be well over an hour before
        # its order goes out (every symbol is analysed before any order is sent). Price the stop and
        # target off the quote now, not the one seen then, or the stop lands at the wrong distance
        # from the real fill and the trade carries more or less risk than it was sized for.
        info = self._broker.get_symbol_info(symbol) or symbol_info
        balance = account.balance if account else 10000.0
        lots = self._broker.calculate_lots(symbol, balance, decision.risk_pct, decision.sl_pips)

        # Current price
        if info:
            entry_price = info.ask if decision.direction == "buy" else info.bid
        else:
            entry_price = 0.0

        sl_price, tp_price = 0.0, 0.0
        if info and entry_price:
            sl_price, tp_price = self._sl_tp_prices(
                entry_price, decision.direction, decision.sl_pips, decision.tp_pips, info
            )

        if self.s.mode == "paper":
            assert self._paper is not None
            ticket = self._paper.place_order(
                symbol=symbol,
                direction=decision.direction,
                lots=lots,
                entry_price=entry_price,
                sl_price=sl_price,
                tp_price=tp_price,
                comment=f"claude:{decision.setup}",
            )
        else:
            ticket = self._broker.place_order(symbol, decision.direction, lots, sl_price, tp_price)

        return lots, entry_price, sl_price, tp_price, ticket

    @staticmethod
    def _sl_tp_prices(
        entry: float,
        direction: str,
        sl_pips: int,
        tp_pips: int,
        info: SymbolInfo,
    ) -> tuple[float, float]:
        pip = info.pip_size
        if direction == "buy":
            sl = round(entry - sl_pips * pip, info.digits)
            tp = round(entry + tp_pips * pip, info.digits)
        else:
            sl = round(entry + sl_pips * pip, info.digits)
            tp = round(entry - tp_pips * pip, info.digits)
        return sl, tp

    def _current_price(self, symbol: str, direction: str) -> float:
        info = self._broker.get_symbol_info(symbol)
        if info is None:
            return 0.0
        return info.bid if direction == "buy" else info.ask

    def _positions(self):
        if self.s.mode == "paper":
            assert self._paper is not None
            # Convert PaperPosition to Position-like objects
            paper_pos = self._paper.get_positions()
            from .broker.metatrader import Position
            from datetime import timezone
            return [
                Position(
                    ticket=p.ticket,
                    symbol=p.symbol,
                    direction=p.direction,
                    lots=p.lots,
                    open_price=p.open_price,
                    sl=p.sl,
                    tp=p.tp,
                    profit=0.0,
                    open_time=p.open_time,
                )
                for p in paper_pos
            ]
        return self._broker.get_positions()

    def _daily_pnl(self) -> float:
        if self.s.mode == "paper":
            assert self._paper is not None
            return self._paper.get_daily_pnl()
        return self._broker.get_daily_pnl()
