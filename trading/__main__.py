"""Entry point for the trading agent.

Usage:
  python -m trading run                           # scan all configured symbols once
  python -m trading run --symbol XAUUSD          # scan one symbol
  python -m trading check                        # validate settings and ping MT5
  python -m trading positions                    # show open positions
  python -m trading history                      # show recent trade decisions
  python -m trading summary                      # paper trading P&L summary
  python -m trading outcomes                     # record closed-trade results; P&L by setup and symbol
  python -m trading study --timeframe H1         # do regime/sweeps/gaps predict the next N bars?
  python -m trading playbooks [NAME]             # list playbooks, or replay one on history and judge it
  python -m trading backtest --from 2026-05-01   # backtest from May to today
  python -m trading backtest --list              # list past backtest runs
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("trading")


def _load() -> tuple:
    from trading.config import Settings
    s = Settings.from_env()
    return s, s.problems()


def cmd_check(args) -> int:
    s, errors = _load()
    if errors:
        for e in errors:
            print(f"  ERROR: {e}")
        return 1
    print(f"Settings OK")
    print(f"  mode={s.mode}  model={s.model}")
    print(f"  symbols={', '.join(s.symbols)}")
    print(f"  max_positions={s.max_positions}  new_opens_per_scan={s.max_new_opens_per_scan}  "
          f"same_direction={s.max_same_direction or 'off'}  max_risk={s.max_risk_pct}%  min_stop={s.min_stop_atr:g}xATR")
    print(f"  daily_loss_limit={s.daily_loss_limit_pct}%  max_total_risk={s.max_total_risk_pct}%  spread_max={s.spread_max_pips} pips")
    if args.ping:
        from trading.broker.metatrader import MT5Broker, MT5Error
        broker = MT5Broker()
        try:
            broker.connect(s.mt5_login, s.mt5_password, s.mt5_server)
            account = broker.get_account()
            if account:
                print(f"  MT5 OK: balance={account.balance:.2f} {account.currency} server={s.mt5_server}")
                ping = broker.ping_ms()
                print(f"  ping_ms: {'not reported by terminal' if ping is None else f'{ping:.0f} ms'}"
                      f"  (halts new opens above {s.max_latency_ms:.0f} ms)")
                now = datetime.now(timezone.utc)
                deals = broker._mt5.history_deals_get(now.replace(hour=0, minute=0, second=0, microsecond=0), now)
                trades = [d for d in deals or () if d.type in (broker._mt5.DEAL_TYPE_BUY, broker._mt5.DEAL_TYPE_SELL)]
                closed = sum(d.profit + d.commission + d.swap for d in trades)
                print(f"  daily_pnl: {broker.get_daily_pnl():.2f}  = closed {closed:.2f} "
                      f"({len(trades)} trade deals since 00:00 UTC) + floating {account.profit:.2f}")
                print(f"  lockout trips at {-account.balance * s.daily_loss_limit_pct / 100:.2f}")
            else:
                print("  MT5 connected but could not read account info")
            broker.disconnect()
        except MT5Error as exc:
            print(f"  MT5 ERROR: {exc}")
            return 1
    return 0


def cmd_run(args) -> int:
    s, errors = _load()
    if errors:
        for e in errors:
            log.error(e)
        return 1

    symbols = [args.symbol] if args.symbol else None

    from trading.runner import TradingRunner
    runner = TradingRunner(s)
    try:
        runner.connect()
        summary = runner.run(symbols=symbols)
        print(summary.line())
        return 0
    except Exception as exc:  # noqa: BLE001
        log.error("Scan failed: %s", exc)
        return 1
    finally:
        runner.disconnect()


def cmd_positions(args) -> int:
    s, _ = _load()
    if s.mode == "paper":
        from trading.paper import PaperEngine
        engine = PaperEngine(s.paper_db)
        positions = engine.get_positions()
        if not positions:
            print("No open paper positions.")
            return 0
        for p in positions:
            print(f"  ticket={p.ticket}  {p.direction:4s}  {p.symbol}  "
                  f"{p.lots} lots @ {p.open_price}  SL={p.sl}  TP={p.tp}")
    else:
        from trading.broker.metatrader import MT5Broker
        broker = MT5Broker()
        broker.connect(s.mt5_login, s.mt5_password, s.mt5_server)
        positions = broker.get_positions()
        broker.disconnect()
        if not positions:
            print("No open live positions.")
            return 0
        for p in positions:
            print(f"  ticket={p.ticket}  {p.direction:4s}  {p.symbol}  "
                  f"{p.lots} lots @ {p.open_price}  P&L={p.profit:.2f}")
    return 0


def cmd_history(args) -> int:
    s, _ = _load()
    from trading.audit import TradeAudit
    audit = TradeAudit(s.state_db)
    rows = audit.recent(limit=args.limit)
    if not rows:
        print("No decisions logged yet.")
        return 0
    for r in rows:
        guardrail = ""
        if r.get("guardrail_notes"):
            notes = json.loads(r["guardrail_notes"] or "[]")
            if notes:
                guardrail = f"  [guardrail: {notes[0]}]"
        print(f"  {r['created_at'][:16]}  {r['symbol']:25s}  "
              f"{r['final_action']:8s}  {r['direction'] or '':4s}  "
              f"conf={r['confidence'] or 0:.0%}  setup={r['setup'] or ''}{guardrail}")
    return 0


def cmd_backtest(args) -> int:
    from datetime import timezone
    s, errors = _load()
    if errors:
        for e in errors:
            log.error(e)
        return 1

    from trading.backtest import BacktestEngine, BacktestStore

    db_path = s.state_db.parent / "backtest.sqlite"

    if args.list:
        store = BacktestStore(db_path)
        runs = store.list_runs()
        if not runs:
            print("No backtest runs yet.")
            return 0
        print(f"{'Run ID':<30}  {'From':>10}  {'To':>10}  {'Closed':>6}  {'W/L':>7}  {'P&L':>8}  {'MaxDD':>7}  {'Errors':>6}")
        print("-" * 95)
        for r in runs:
            syms = json.loads(r.get("symbols") or "[]")
            closed = r.get("trades_closed") or 0
            wins   = r.get("wins") or 0
            wl     = f"{wins}/{closed}"
            print(f"{r['run_id']:<30}  {(r.get('date_from') or '')[:10]:>10}  "
                  f"{(r.get('date_to') or '')[:10]:>10}  {closed:>6}  {wl:>7}  "
                  f"{r.get('total_pnl') or 0:>8.2f}  "
                  f"{r.get('max_drawdown') or 0:>7.2f}  "
                  f"{r.get('errors') or 0:>6}")
        return 0

    if args.show:
        store = BacktestStore(db_path)
        run_id = args.show
        summary = store.summary(run_id)
        if not summary:
            print(f"Backtest run {run_id!r} not found.")
            return 1
        trades = store.trades(run_id)
        print(f"\nBacktest: {run_id}")
        print(f"  Period:          {summary.get('date_from','?')[:10]} to {summary.get('date_to','?')[:10]}")
        print(f"  Initial balance: {summary.get('initial_balance', 0):.2f}")
        print(f"  Final balance:   {summary.get('final_balance', 0):.2f}")
        print(f"  Total P&L:       {summary.get('total_pnl', 0):.2f}")
        closed = summary.get('trades_closed') or 0
        wins   = summary.get('wins') or 0
        win_pct = f"{wins/closed:.0%}" if closed else "n/a"
        print(f"  Trades:          {closed} closed  {wins} wins  {win_pct} win rate")
        print(f"  Max drawdown:    {summary.get('max_drawdown', 0):.2f}")
        print(f"  Errors:          {summary.get('errors', 0)}")
        if trades:
            print(f"\n  {'Symbol':<25}  {'Dir':>4}  {'Lots':>6}  {'Entry':>9}  {'Exit':>9}  {'P&L':>8}  {'Reason':>6}  Open time")
            print(f"  {'-'*93}")
            for t in trades:
                print(f"  {t['symbol']:<25}  {t['direction']:>4}  {t['lots']:>6.3f}  "
                      f"{t['open_price']:>9.4f}  {t.get('close_price') or 0:>9.4f}  "
                      f"{t.get('pnl') or 0:>8.2f}  {t.get('close_reason') or '':>6}  "
                      f"{(t.get('open_time') or '')[:16]}")
        return 0

    # ---- run a new backtest ---------------------------------------------------
    today = datetime.now(tz=timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    def _parse_date(s_arg: str) -> datetime:
        return datetime.strptime(s_arg, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    date_from = _parse_date(args.date_from) if args.date_from else today.replace(month=5, day=1)
    date_to   = _parse_date(args.date_to)   if args.date_to   else today

    if args.symbol:
        symbols = [args.symbol]
    elif args.symbols:
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    else:
        symbols = None
    interval = float(args.interval)
    balance  = float(args.balance)

    log.info("Starting backtest  %s → %s  interval=%gd  balance=%.2f",
             date_from.date(), date_to.date(), interval, balance)

    engine = BacktestEngine(s)
    run_id = engine.run(
        date_from=date_from,
        date_to=date_to,
        symbols=symbols,
        interval_days=interval,
        initial_balance=balance,
    )

    # Print summary
    store = BacktestStore(db_path)
    summary = store.summary(run_id)
    closed = summary.get("trades_closed") or 0
    wins   = summary.get("wins") or 0
    win_pct = f"{wins/closed:.0%}" if closed else "n/a"
    print(f"\nBacktest complete: {run_id}")
    print(f"  P&L:       {summary.get('total_pnl', 0):.2f}")
    print(f"  Trades:    {closed} closed  ({wins} wins, {win_pct} win rate)")
    print(f"  Max DD:    {summary.get('max_drawdown', 0):.2f}")
    print(f"  Errors:    {summary.get('errors', 0)}")
    print(f"\n  Run `python -m trading backtest --show {run_id}` for full trade log")
    return 0


def cmd_study(args) -> int:
    """Test whether regime, sweeps, gaps and breaks predict the next N bars on historical candles."""
    s, errors = _load()
    if errors:
        for e in errors:
            log.error(e)
        return 1

    from trading.broker.metatrader import MT5Broker, MT5Error
    from trading.geometry import ATR_PERIOD
    from trading.indicators import atr as atr_of
    from trading.study import run

    symbols = [x.strip() for x in args.symbols.split(",") if x.strip()] if args.symbols else list(s.symbols)
    horizons = tuple(int(h) for h in args.horizons.split(","))
    broker = MT5Broker()
    candles, cost = {}, {}
    try:
        broker.connect(s.mt5_login, s.mt5_password, s.mt5_server)
        for sym in symbols:
            data = broker.get_candles(sym, args.timeframe, args.bars)
            if len(data) < args.window + max(horizons) + 50:
                log.warning("%s: only %d bars, skipped", sym, len(data))
                continue
            candles[sym] = data
            info = broker.get_symbol_info(sym)
            atr = atr_of([c.high for c in data], [c.low for c in data], [c.close for c in data], ATR_PERIOD)
            if info and atr:
                cost[sym] = (info.ask - info.bid) / atr
            log.info("%s: %d bars (%s to %s)", sym, len(data), data[0].time.date(), data[-1].time.date())
    except MT5Error as exc:
        log.error("MT5: %s", exc)
        return 1
    finally:
        broker.disconnect()

    if not candles:
        log.error("No symbol had enough history.")
        return 1
    result = run(candles, args.timeframe, horizons, window=args.window, step=args.step, cost_atr=cost)
    print(result.text(top=args.top))
    print(f"\nFull results: {result.save(s.state_db.parent)}")
    return 0


def cmd_playbooks(args) -> int:
    """List the registered playbooks, or replay one over historical candles and judge it by the fixed gate."""
    from trading.playbooks import REGISTRY
    if not args.playbook or args.playbook == "list":
        if not REGISTRY:
            print("No playbooks registered yet.")
        for name, cls in sorted(REGISTRY.items()):
            print(f"  {name:<24} {cls.timeframe}  for: {', '.join(cls.asset_classes)}  "
                  f"(stop {cls.stop_atr:g} ATR, target {cls.target_atr:g} ATR, {cls.max_bars} bars max)")
        return 0
    if args.playbook not in REGISTRY:
        log.error("Unknown playbook %r. Registered: %s", args.playbook, ", ".join(sorted(REGISTRY)) or "none")
        return 1

    s, errors = _load()
    if errors:
        for e in errors:
            log.error(e)
        return 1

    from trading.broker.metatrader import MT5Broker, MT5Error
    from trading.playbooks.evaluate import run_playbook
    from trading.study import asset_class

    playbook = REGISTRY[args.playbook]()
    timeframe = args.timeframe or playbook.timeframe
    wanted = [x.strip() for x in args.symbols.split(",") if x.strip()] if args.symbols else list(s.symbols)
    symbols = [x for x in wanted if asset_class(x) in playbook.asset_classes]
    if not symbols:
        log.error("None of the symbols is in this playbook's asset classes (%s).", ", ".join(playbook.asset_classes))
        return 1

    broker = MT5Broker()
    candles, spreads = {}, {}
    try:
        broker.connect(s.mt5_login, s.mt5_password, s.mt5_server)
        for sym in symbols:
            data = broker.get_candles(sym, timeframe, args.bars)
            if len(data) < playbook.warmup + playbook.max_bars + 50:
                log.warning("%s: only %d bars, skipped", sym, len(data))
                continue
            candles[sym] = data
            info = broker.get_symbol_info(sym)
            spreads[sym] = (info.ask - info.bid) if info else 0.0
            log.info("%s: %d bars (%s to %s)", sym, len(data), data[0].time.date(), data[-1].time.date())
    except MT5Error as exc:
        log.error("MT5: %s", exc)
        return 1
    finally:
        broker.disconnect()
    if not candles:
        log.error("No symbol had enough history.")
        return 1

    report = run_playbook(playbook, candles, spreads, seed=args.seed)
    print(report.text())
    print(f"\nFull results: {report.save(s.state_db.parent)}")
    return 0


def cmd_summary(args) -> int:
    s, _ = _load()
    if s.mode == "paper":
        from trading.paper import PaperEngine
        engine = PaperEngine(s.paper_db)
        data = engine.summary()
    else:
        from trading.audit import TradeAudit
        data = TradeAudit(s.state_db).stats(mode=s.mode)
    for k, v in data.items():
        print(f"  {k}: {v}")
    return 0


def cmd_outcomes(args) -> int:
    """Record results for closed trades, then print them overall and by setup and symbol."""
    s, errors = _load()
    if errors:
        for e in errors:
            log.error(e)
        return 1

    from trading.audit import TradeAudit
    from trading.runner import TradingRunner

    runner = TradingRunner(s)
    try:
        runner.connect_broker()
        adopted = runner.adopt_orphans()
        recorded = runner.reconcile_outcomes()
    except Exception as exc:  # noqa: BLE001
        log.error("Could not reconcile outcomes: %s", exc)
        return 1
    finally:
        runner.disconnect()

    audit = TradeAudit(s.state_db)
    print(f"Adopted {adopted} position(s) missing from the database; recorded {recorded} new result(s).\n")
    for k, v in audit.stats(mode=s.mode).items():
        if k != "actions":
            print(f"  {k}: {v}")
    exits = audit.exit_stats(mode=s.mode)
    if exits["measured"]:
        print(f"\nExit management, in multiples of each trade's initial risk (R), over {exits['measured']} measured trade(s):")
        print(f"  avg best point:   {exits['avg_best_r']:+.2f}R    avg worst point: {exits['avg_worst_r']:+.2f}R    avg final: {exits['avg_final_r']:+.2f}R")
        t_r = exits["final_r_t_stat"]
        print(f"  t-stat of average final R: {'n/a (needs 2+ trades with variation)' if t_r is None else f'{t_r:+.2f}'}"
              "   (the yardstick for an edge: positive average R with t >= 2 after ~100 trades)")
        kept = f"{exits['kept_pct']:.0f}%" if exits["kept_pct"] is not None else "n/a"
        print(f"  kept {kept} of the best open profit (below 0% means trades that were ahead ended as losses); "
              f"{exits['ever_1r']} trade(s) reached +1R, {exits['gave_back']} of them ended as a loss")
    for by in ("setup", "symbol", "stop_timeframe"):
        rows = audit.breakdown(by, mode=s.mode)
        if not rows:
            continue
        print(f"\nBy {by}:")
        print(f"  {by:<28} {'closed':>6} {'wins':>5} {'P&L':>10} {'avg':>8}")
        for r in rows:
            print(f"  {r['key']:<28} {r['closed']:>6} {r['wins']:>5} {r['pnl']:>10.2f} {r['avg_pnl']:>8.2f}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m trading", description="NMB Trading Agent")
    sub = parser.add_subparsers(dest="cmd")

    p_check = sub.add_parser("check", help="Validate settings and optionally ping MT5")
    p_check.add_argument("--ping", action="store_true", help="Connect to MT5 and read account info")

    p_run = sub.add_parser("run", help="Run one scan over all (or one) symbol(s)")
    p_run.add_argument("--symbol", help="Scan only this symbol")

    sub.add_parser("positions", help="List open positions")

    p_hist = sub.add_parser("history", help="Show recent trade decisions")
    p_hist.add_argument("--limit", type=int, default=20, help="Number of rows to show")

    sub.add_parser("summary", help="Paper trading P&L summary")
    p_pb = sub.add_parser("playbooks", help="List playbooks, or replay one on history and judge it by the fixed gate")
    p_pb.add_argument("playbook", nargs="?", help="Playbook name, or 'list' (default)")
    p_pb.add_argument("--timeframe", help="Candle timeframe (default: the playbook's own)")
    p_pb.add_argument("--bars", type=int, default=20000, help="History to load per symbol (default 20000)")
    p_pb.add_argument("--symbols", help="Comma-separated symbols (default: configured list, filtered to the playbook's asset classes)")
    p_pb.add_argument("--seed", default="0", help="Seed for the placebo (default 0)")
    p_study = sub.add_parser("study", help="Test whether regime, sweeps and gaps predict the next N bars")
    p_study.add_argument("--timeframe", default="H1", help="Candle timeframe (default H1)")
    p_study.add_argument("--bars", type=int, default=15000, help="History to load per symbol (default 15000)")
    p_study.add_argument("--symbols", help="Comma-separated symbols (default: configured list)")
    p_study.add_argument("--horizons", default="4,12,24", help="Bars ahead to measure (default 4,12,24)")
    p_study.add_argument("--window", type=int, default=300, help="Bars the engine sees at each step (default 300)")
    p_study.add_argument("--step", type=int, default=2, help="Bars between observations (default 2)")
    p_study.add_argument("--top", type=int, default=20, help="Rows in the strongest-signals table (default 20)")
    sub.add_parser("outcomes", help="Record results of closed trades; show P&L by setup and symbol")

    p_bt = sub.add_parser("backtest", help="Historical backtest using MT5 data")
    p_bt.add_argument("--from",   dest="date_from", metavar="YYYY-MM-DD",
                      help="Start date (default: May 1 of current year)")
    p_bt.add_argument("--to",     dest="date_to",   metavar="YYYY-MM-DD",
                      help="End date (default: today)")
    p_bt.add_argument("--symbol", help="Backtest only this one symbol")
    p_bt.add_argument("--symbols", help="Comma-separated symbol list (overrides config)")
    p_bt.add_argument("--interval", type=float, default=7.0,
                      metavar="DAYS", help="Days between decision timestamps (default: 7)")
    p_bt.add_argument("--balance", type=float, default=10000.0,
                      help="Starting paper balance (default: 10000)")
    p_bt.add_argument("--list",   action="store_true", help="List past backtest runs")
    p_bt.add_argument("--show",   metavar="RUN_ID",
                      help="Show full trade log for a past run")

    args = parser.parse_args()

    if args.cmd == "check":
        return cmd_check(args)
    if args.cmd == "run":
        return cmd_run(args)
    if args.cmd == "positions":
        return cmd_positions(args)
    if args.cmd == "history":
        return cmd_history(args)
    if args.cmd == "summary":
        return cmd_summary(args)
    if args.cmd == "backtest":
        return cmd_backtest(args)
    if args.cmd == "outcomes":
        return cmd_outcomes(args)
    if args.cmd == "study":
        return cmd_study(args)
    if args.cmd == "playbooks":
        return cmd_playbooks(args)

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
