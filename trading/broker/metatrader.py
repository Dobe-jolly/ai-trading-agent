"""MetaTrader 5 broker connector.

Wraps the MetaTrader5 Python package. All price data and account information
come through here. The rest of the trading module never imports MetaTrader5 directly.

Requires the MT5 terminal to be running locally. On connection failure every
method returns an empty/error result rather than raising, so a disconnection
during a scan does not crash the agent loop.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

log = logging.getLogger(__name__)

# A trade whose smallest possible size risks more than this multiple of the intended amount is
# too big for the account and is refused (a 2% trade must not quietly become a 5% one).
MIN_LOT_RISK_TOLERANCE = 1.5

# Tags every order the bot sends, so its trades can be told apart from ones placed by hand.
MAGIC = 20260001

# Seconds to wait before each reconnect attempt, about two minutes in all.
DEFAULT_BACKOFF_S = (1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0, 30.0)

# MetaTrader5 timeframe constants (mirrors mt5.TIMEFRAME_*)
TIMEFRAMES: dict[str, int] = {
    "M1": 1, "M5": 5, "M15": 15, "M30": 30,
    "H1": 16385, "H4": 16388, "D1": 16408,
    "W1": 32769, "MN1": 49153,
}


@dataclass
class Candle:
    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "time": self.time.strftime("%Y-%m-%d %H:%M"),
            "open": round(self.open, 6),
            "high": round(self.high, 6),
            "low": round(self.low, 6),
            "close": round(self.close, 6),
            "volume": self.volume,
        }


@dataclass
class SymbolInfo:
    name: str
    digits: int
    point: float
    trade_tick_size: float
    trade_tick_value: float
    trade_contract_size: float
    volume_min: float
    volume_max: float
    volume_step: float
    spread: int
    bid: float
    ask: float
    # What a one-pip move on one lot really pays, measured with MT5's own profit calculator.
    # 0 means it could not be measured. Some synthetic indices report tick values that are
    # off by 10x or more from what the broker actually charges, so this is preferred.
    measured_pip_value: float = 0.0

    @property
    def pip_size(self) -> float:
        """One pip in price units (0.0001 for 5-digit forex, 0.01 for JPY pairs)."""
        if self.digits in (3, 5):
            return self.point * 10
        return self.point

    @property
    def pip_value_per_lot(self) -> float:
        """Monetary value of 1 pip for 1 standard lot, in account currency.

        The measured value when there is one, otherwise worked out from the tick size and value
        the symbol reports (which is wrong for some synthetics).
        """
        if self.measured_pip_value > 0:
            return self.measured_pip_value
        if self.trade_tick_size == 0:
            return 0.0
        return (self.pip_size / self.trade_tick_size) * self.trade_tick_value

    @property
    def sizing_pip_value(self) -> float:
        """The pip value to size trades with: the measured one, or, failing that, the reported one
        floored at 0.001 so a bad (tiny) reported value can't make a position far too large."""
        if self.measured_pip_value > 0:
            return self.measured_pip_value
        return max(self.pip_value_per_lot, 0.001)

    def spread_pips(self) -> float:
        if self.pip_size == 0:
            return 0.0
        return round((self.ask - self.bid) / self.pip_size, 1)


@dataclass
class Position:
    ticket: int
    symbol: str
    direction: str   # "buy" or "sell"
    lots: float
    open_price: float
    sl: float
    tp: float
    profit: float
    open_time: datetime
    comment: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "ticket": self.ticket,
            "symbol": self.symbol,
            "direction": self.direction,
            "lots": self.lots,
            "open_price": self.open_price,
            "sl": self.sl,
            "tp": self.tp,
            "profit": round(self.profit, 2),
            "open_time": self.open_time.strftime("%Y-%m-%d %H:%M"),
        }


@dataclass
class BotEntry:
    """A position the bot opened, as the broker's deal history records it."""
    position_id: int         # equals the order ticket on a hedging account, which is what the database stores
    symbol: str
    direction: str
    lots: float
    price: float
    opened_at: datetime


@dataclass
class ClosedTrade:
    ticket: int
    profit: float            # net of commission, swap and fees, entry deal included
    close_price: float
    closed_at: datetime
    open_price: float = 0.0              # actual fill of the entry deal (0 when unknown)
    opened_at: Optional[datetime] = None


@dataclass
class AccountState:
    balance: float
    equity: float
    margin: float
    free_margin: float
    margin_level: float   # equity / margin * 100, or 0 if no margin
    profit: float         # unrealised P&L on all open positions
    currency: str = "USD"

    def as_dict(self) -> dict[str, Any]:
        return {
            "balance": round(self.balance, 2),
            "equity": round(self.equity, 2),
            "free_margin": round(self.free_margin, 2),
            "margin_level_pct": round(self.margin_level, 1),
            "open_profit": round(self.profit, 2),
            "currency": self.currency,
        }


class MT5Error(RuntimeError):
    """MT5 terminal returned an error or is unreachable."""


class MT5Broker:
    """Thin wrapper around the MetaTrader5 Python package.

    Call connect() once at startup. Every call first checks the broker link and, if it
    dropped, reconnects with exponential backoff (see ensure_connected). If the broker
    stays unreachable, methods return sensible defaults and order methods raise, so a long
    outage degrades one scan instead of crashing the loop.
    """

    def __init__(self, backoff: tuple[float, ...] = DEFAULT_BACKOFF_S, cooldown_s: float = 120.0) -> None:
        self._mt5: Any = None
        self._connected = False                 # True once connect() has succeeded and until disconnect()
        self._creds: Optional[tuple[int, str, str]] = None
        self._backoff = backoff
        self._cooldown = cooldown_s
        self._retry_after = 0.0                 # no reconnect attempts before this (monotonic seconds)
        self._sleep = time.sleep                # replaced in tests
        self._clock = time.monotonic

    # ---- lifecycle ---------------------------------------------------------------

    def connect(self, login: int, password: str, server: str) -> None:
        try:
            import MetaTrader5 as mt5
            self._mt5 = mt5
        except ImportError as exc:
            raise MT5Error("MetaTrader5 package not installed. Run: pip install MetaTrader5") from exc

        self._creds = (login, password, server)
        if not self._initialize():
            code, msg = self._mt5.last_error()
            raise MT5Error(f"MT5 initialize failed ({code}): {msg}")
        self._connected = True
        info = self._mt5.account_info()
        log.info("MT5 connected: %s %s (server=%s, balance=%.2f %s)",
                 info.name if info else "?", login, server,
                 info.balance if info else 0, info.currency if info else "")

    def disconnect(self) -> None:
        if self._mt5 and self._connected:
            self._mt5.shutdown()
            self._connected = False

    def _initialize(self) -> bool:
        login, password, server = self._creds
        return bool(self._mt5.initialize(login=login, password=password, server=server))

    def _link_ok(self) -> bool:
        """True while the terminal is connected to the broker's trade server."""
        info = self._mt5.terminal_info()
        return bool(info is not None and getattr(info, "connected", True))

    def ensure_connected(self) -> bool:
        """Make sure the broker link is up, reconnecting with exponential backoff if it dropped.

        Returns False without waiting when the link is down and a recent reconnect already
        failed, so one outage doesn't stall every call in a scan. An order that was in flight
        when the link dropped is never retried here, since it may have gone through.
        """
        if not self._connected:
            return False
        if self._link_ok():
            return True
        if self._clock() < self._retry_after:
            return False
        log.warning("MT5 link is down; reconnecting")
        for attempt, delay in enumerate(self._backoff, start=1):
            self._sleep(delay)
            try:
                self._mt5.shutdown()
            except Exception:  # noqa: BLE001 — a dead terminal may refuse even this
                pass
            if self._initialize() and self._link_ok():
                log.info("MT5 reconnected (attempt %d/%d)", attempt, len(self._backoff))
                return True
            log.warning("MT5 reconnect attempt %d/%d failed", attempt, len(self._backoff))
        self._retry_after = self._clock() + self._cooldown
        log.error("MT5 unreachable after %d attempts; not retrying for %.0fs", len(self._backoff), self._cooldown)
        return False

    def ping(self) -> bool:
        if not self._mt5:
            return False
        return self._mt5.account_info() is not None

    def ping_ms(self) -> Optional[float]:
        """Round-trip time to the broker's trade server, or None when the terminal doesn't report it."""
        if not self.ensure_connected():
            return None
        info = self._mt5.terminal_info()
        micros = getattr(info, "ping_last", None) if info else None
        # The terminal reports -1 (or 0) before it has measured a ping.
        return micros / 1000.0 if micros and micros > 0 else None

    def get_daily_pnl(self) -> float:
        """Closed P&L since 00:00 UTC (profit + commission + swap on trade deals) plus floating P&L.

        Deposits and withdrawals are balance deals, not trades, and are excluded. Falls back to
        floating P&L alone when the deal history cannot be read.
        """
        account = self.get_account()
        floating = account.profit if account else 0.0
        if not self.ensure_connected():
            return floating
        now = datetime.now(timezone.utc)
        deals = self._mt5.history_deals_get(now.replace(hour=0, minute=0, second=0, microsecond=0), now)
        if deals is None:
            log.warning("MT5 deal history unavailable; daily P&L counts open positions only")
            return floating
        trade_types = (self._mt5.DEAL_TYPE_BUY, self._mt5.DEAL_TYPE_SELL)
        closed = sum(d.profit + d.commission + d.swap for d in deals if d.type in trade_types)
        return closed + floating

    # ---- market data -------------------------------------------------------------

    def get_candles(self, symbol: str, timeframe: str, count: int = 100) -> list[Candle]:
        if not self.ensure_connected():
            return []
        tf = TIMEFRAMES.get(timeframe.upper())
        if tf is None:
            raise MT5Error(f"Unknown timeframe {timeframe!r}. Use: {', '.join(TIMEFRAMES)}")
        rates = self._mt5.copy_rates_from_pos(symbol, tf, 0, count)
        if rates is None or len(rates) == 0:
            code, msg = self._mt5.last_error()
            log.warning("get_candles %s %s: (%d) %s", symbol, timeframe, code, msg)
            return []
        candles = []
        for r in rates:
            candles.append(Candle(
                time=datetime.fromtimestamp(r["time"], tz=timezone.utc),
                open=float(r["open"]),
                high=float(r["high"]),
                low=float(r["low"]),
                close=float(r["close"]),
                volume=int(r["tick_volume"]),
            ))
        return candles

    def get_candles_range(self, symbol: str, timeframe: str,
                          date_from: datetime, date_to: datetime) -> list[Candle]:
        """Fetch all candles in a date range — used by the backtester."""
        if not self.ensure_connected():
            return []
        tf = TIMEFRAMES.get(timeframe.upper())
        if tf is None:
            return []
        rates = self._mt5.copy_rates_range(symbol, tf, date_from, date_to)
        if rates is None or len(rates) == 0:
            return []
        return [
            Candle(
                time=datetime.fromtimestamp(r["time"], tz=timezone.utc),
                open=float(r["open"]),
                high=float(r["high"]),
                low=float(r["low"]),
                close=float(r["close"]),
                volume=int(r["tick_volume"]),
            )
            for r in rates
        ]

    def get_symbol_info(self, symbol: str) -> Optional[SymbolInfo]:
        if not self.ensure_connected():
            return None
        info = self._mt5.symbol_info_tick(symbol)
        sym = self._mt5.symbol_info(symbol)
        if info is None or sym is None:
            return None
        out = SymbolInfo(
            name=symbol,
            digits=sym.digits,
            point=sym.point,
            trade_tick_size=sym.trade_tick_size,
            trade_tick_value=sym.trade_tick_value,
            trade_contract_size=sym.trade_contract_size,
            volume_min=sym.volume_min,
            volume_max=sym.volume_max,
            volume_step=sym.volume_step,
            spread=sym.spread,
            bid=float(info.bid),
            ask=float(info.ask),
        )
        out.measured_pip_value = self._measure_pip_value(symbol, out)
        return out

    def _measure_pip_value(self, symbol: str, info: SymbolInfo) -> float:
        """Ask MT5 what one lot earns when price moves up, and scale that to one pip.

        The move is 1% of the price, large enough that rounding to cents doesn't swamp the
        answer on symbols with tiny pips. Returns 0.0 when MT5 can't say.
        """
        price, pip = info.ask, info.pip_size
        if price <= 0 or pip <= 0:
            return 0.0
        move = max(pip, price * 0.01)
        try:
            profit = self._mt5.order_calc_profit(self._mt5.ORDER_TYPE_BUY, symbol, 1.0, price, price + move)
        except Exception:  # noqa: BLE001 — an unsupported symbol must not break pricing
            return 0.0
        return abs(float(profit)) / move * pip if profit else 0.0

    # ---- account -----------------------------------------------------------------

    def get_account(self) -> Optional[AccountState]:
        if not self.ensure_connected():
            return None
        info = self._mt5.account_info()
        if info is None:
            return None
        margin_level = 0.0
        if info.margin > 0:
            margin_level = (info.equity / info.margin) * 100
        return AccountState(
            balance=float(info.balance),
            equity=float(info.equity),
            margin=float(info.margin),
            free_margin=float(info.margin_free),
            margin_level=margin_level,
            profit=float(info.profit),
            currency=str(info.currency),
        )

    # ---- positions ---------------------------------------------------------------

    def get_positions(self, symbol: Optional[str] = None) -> list[Position]:
        if not self.ensure_connected():
            return []
        positions = self._mt5.positions_get(symbol=symbol) if symbol else self._mt5.positions_get()
        if positions is None:
            return []
        result = []
        for p in positions:
            result.append(Position(
                ticket=int(p.ticket),
                symbol=str(p.symbol),
                direction="buy" if p.type == 0 else "sell",
                lots=float(p.volume),
                open_price=float(p.price_open),
                sl=float(p.sl),
                tp=float(p.tp),
                profit=float(p.profit),
                open_time=datetime.fromtimestamp(p.time, tz=timezone.utc),
                comment=str(p.comment),
            ))
        return result

    def get_bot_entries(self, since: datetime) -> list[BotEntry]:
        """Positions this bot opened since `since`, open or already closed.

        Reads entry deals carrying the bot's magic number, so trades opened by hand, and
        deposits and withdrawals, are left out. Used to find orders that reached the broker
        but never made it into the database.
        """
        if not self.ensure_connected():
            return []
        deals = self._mt5.history_deals_get(since, datetime.now(timezone.utc))
        if not deals:
            return []
        sides = {self._mt5.DEAL_TYPE_BUY: "buy", self._mt5.DEAL_TYPE_SELL: "sell"}
        return [
            BotEntry(int(d.position_id), str(d.symbol), sides[d.type], float(d.volume), float(d.price),
                     datetime.fromtimestamp(d.time, tz=timezone.utc))
            for d in deals
            if d.entry == self._mt5.DEAL_ENTRY_IN and d.magic == MAGIC and d.type in sides
        ]

    def get_closed_trade(self, ticket: int) -> Optional[ClosedTrade]:
        """The final result of a position, or None while it is still open or has no exit deal.

        Covers every way a position can end (stop, target, manual close, stop-out), since
        it reads the deal history, not what this bot sent.
        """
        if not self.ensure_connected() or self._mt5.positions_get(ticket=ticket):
            return None
        deals = self._mt5.history_deals_get(position=ticket)
        if not deals:
            return None
        exit_entries = (self._mt5.DEAL_ENTRY_OUT, self._mt5.DEAL_ENTRY_INOUT, self._mt5.DEAL_ENTRY_OUT_BY)
        exits = [d for d in deals if d.entry in exit_entries]
        if not exits:
            return None
        net = sum(d.profit + d.commission + d.swap + getattr(d, "fee", 0.0) for d in deals)
        last = max(exits, key=lambda d: d.time)
        first_in = min((d for d in deals if d.entry == self._mt5.DEAL_ENTRY_IN), key=lambda d: d.time, default=None)
        return ClosedTrade(
            ticket, float(net), float(last.price), datetime.fromtimestamp(last.time, tz=timezone.utc),
            open_price=float(first_in.price) if first_in else 0.0,
            opened_at=datetime.fromtimestamp(first_in.time, tz=timezone.utc) if first_in else None,
        )

    def get_price_excursion(
        self, symbol: str, direction: str, entry: float, opened_at: datetime, closed_at: datetime,
    ) -> Optional[tuple[float, float]]:
        """Best and worst the price got for a trade while it was open, as (favourable, adverse)
        distances from `entry` in price units, both >= 0.

        Uses the finest candles the terminal still has (M1, then M5, then M15). The window is the
        trade's own lifetime, so nothing before the entry or after the exit leaks in. Returns None
        when no candles cover it. The terminal's bar times are assumed to be UTC, which holds for
        Deriv's servers.
        """
        if not self.ensure_connected():
            return None
        for tf in ("M1", "M5", "M15"):
            rates = self._mt5.copy_rates_range(symbol, TIMEFRAMES[tf], opened_at, closed_at)
            if rates is not None and len(rates) > 0:
                break
        else:
            return None
        high, low = max(float(r["high"]) for r in rates), min(float(r["low"]) for r in rates)
        up, down = max(0.0, high - entry), max(0.0, entry - low)
        return (up, down) if direction == "buy" else (down, up)

    # ---- order execution ---------------------------------------------------------

    def _filling_mode(self, symbol: str) -> int:
        """Return the best supported filling mode for a symbol (FOK > IOC > RETURN)."""
        info = self._mt5.symbol_info(symbol)
        mask = getattr(info, "filling_mode", 0) if info else 0
        if mask & 1:   # FOK
            return self._mt5.ORDER_FILLING_FOK
        if mask & 2:   # IOC
            return self._mt5.ORDER_FILLING_IOC
        return self._mt5.ORDER_FILLING_RETURN

    def place_order(
        self,
        symbol: str,
        direction: str,
        lots: float,
        sl_price: float,
        tp_price: float,
        comment: str = "nmbz-trader",
    ) -> int:
        """Place a market order. Returns the position ticket on success."""
        if not self.ensure_connected():
            raise MT5Error("Not connected to MT5")
        order_type = self._mt5.ORDER_TYPE_BUY if direction == "buy" else self._mt5.ORDER_TYPE_SELL
        tick = self._mt5.symbol_info_tick(symbol)
        if tick is None:
            raise MT5Error(f"Cannot get tick for {symbol}")
        price = tick.ask if direction == "buy" else tick.bid
        request = {
            "action": self._mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": lots,
            "type": order_type,
            "price": price,
            "sl": sl_price,
            "tp": tp_price,
            "deviation": 20,
            "magic": MAGIC,
            "comment": comment,
            "type_time": self._mt5.ORDER_TIME_GTC,
            "type_filling": self._filling_mode(symbol),
        }
        result = self._mt5.order_send(request)
        if result is None or result.retcode != self._mt5.TRADE_RETCODE_DONE:
            code = result.retcode if result else -1
            msg = result.comment if result else "no result"
            raise MT5Error(f"Order failed (retcode={code}): {msg}")
        log.info("Order placed: %s %s %.2f lots @ %.5f (ticket=%s)", direction, symbol, lots, price, result.order)
        return int(result.order)

    def close_position(self, ticket: int) -> bool:
        """Close a position by ticket. Returns True on success."""
        if not self.ensure_connected():
            raise MT5Error("Not connected to MT5")
        position = self._mt5.positions_get(ticket=ticket)
        if not position:
            raise MT5Error(f"Position {ticket} not found")
        pos = position[0]
        close_type = self._mt5.ORDER_TYPE_SELL if pos.type == 0 else self._mt5.ORDER_TYPE_BUY
        tick = self._mt5.symbol_info_tick(pos.symbol)
        if tick is None:
            raise MT5Error(f"Cannot get tick for {pos.symbol}")
        price = tick.bid if pos.type == 0 else tick.ask
        request = {
            "action": self._mt5.TRADE_ACTION_DEAL,
            "symbol": pos.symbol,
            "volume": pos.volume,
            "type": close_type,
            "position": ticket,
            "price": price,
            "deviation": 20,
            "magic": MAGIC,
            "comment": "nmbz-trader close",
            "type_time": self._mt5.ORDER_TIME_GTC,
            "type_filling": self._filling_mode(pos.symbol),
        }
        result = self._mt5.order_send(request)
        if result is None or result.retcode != self._mt5.TRADE_RETCODE_DONE:
            code = result.retcode if result else -1
            msg = result.comment if result else "no result"
            raise MT5Error(f"Close failed (retcode={code}): {msg}")
        log.info("Position %d closed", ticket)
        return True

    def modify_position(self, ticket: int, sl_price: float, tp_price: float) -> bool:
        """Move SL/TP on an open position."""
        if not self.ensure_connected():
            raise MT5Error("Not connected to MT5")
        request = {
            "action": self._mt5.TRADE_ACTION_SLTP,
            "position": ticket,
            "sl": sl_price,
            "tp": tp_price,
        }
        result = self._mt5.order_send(request)
        if result is None or result.retcode != self._mt5.TRADE_RETCODE_DONE:
            code = result.retcode if result else -1
            msg = result.comment if result else "no result"
            raise MT5Error(f"Modify failed (retcode={code}): {msg}")
        return True

    # ---- lot calculation ----------------------------------------------------------

    def calculate_lots(self, symbol: str, account_balance: float, risk_pct: float, sl_pips: int) -> float:
        """Calculate lot size so that sl_pips of adverse move = risk_pct% of balance."""
        info = self.get_symbol_info(symbol)
        if info is None or info.pip_value_per_lot == 0 or sl_pips <= 0:
            sym = self._mt5.symbol_info(symbol) if self._mt5 else None
            return float(sym.volume_min) if sym else 0.01
        risk_amount = account_balance * (risk_pct / 100.0)
        pvpl = info.sizing_pip_value
        raw_lots = risk_amount / (sl_pips * pvpl)
        step = info.volume_step
        lots = round(round(raw_lots / step) * step, 8)
        lots = max(info.volume_min, min(info.volume_max, lots))
        # If even this size risks well over what was intended (the minimum lot may be too big for
        # the account), the guardrails refuse the trade beforehand; this keeps the smallest size.
        if lots * sl_pips * pvpl > risk_amount * MIN_LOT_RISK_TOLERANCE:
            lots = info.volume_min
        return lots
