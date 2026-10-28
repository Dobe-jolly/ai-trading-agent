"""Trading agent settings loaded from .env.

Shared names (ANTHROPIC_API_KEY, AGENT_MODEL) mean the same as in the desk agents.
Prefix with TRADING_ to override for the trading agent only.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
DESK = "trading"
PREFIX = "TRADING"
VALID_MODES = ("paper", "live")
VALID_TIMEFRAMES = ("M1", "M5", "M15", "M30", "H1", "H4", "D1")

# Curated backtest set — fast enough to finish in a day
DEFAULT_SYMBOLS = (
    "EURUSD,GBPUSD,XAUUSD,"
    "Volatility 75 Index,Volatility 25 Index,"
    "Step Index,"
    "Volatility 100 (1s) Index,"
    "Boom 1000 Index,"
    "Crash 1000 Index"
)

# Full live-trading symbol list — set TRADING_SYMBOLS in .env to use these
LIVE_SYMBOLS = (
    "EURUSD,GBPUSD,XAUUSD,"
    "Volatility 10 Index,Volatility 25 Index,Volatility 50 Index,"
    "Volatility 75 Index,Volatility 100 Index,"
    "Volatility 10 (1s) Index,Volatility 25 (1s) Index,Volatility 50 (1s) Index,"
    "Volatility 75 (1s) Index,Volatility 100 (1s) Index,"
    "Step Index,"
    "Boom 300 Index,Boom 500 Index,Boom 1000 Index,"
    "Crash 300 Index,Crash 500 Index,Crash 1000 Index"
)


@dataclass(frozen=True)
class Settings:
    # MT5 connection
    mt5_login: int
    mt5_password: str
    mt5_server: str

    # Claude
    anthropic_api_key: Optional[str]
    model: str
    max_agent_turns: int
    max_tokens: int

    # Behaviour
    mode: str                    # paper | live
    symbols: list                # which symbols to scan each run
    primary_timeframe: str       # H1 default
    confidence_threshold: float
    max_positions: int
    max_new_opens_per_scan: int  # new positions allowed in one scan, across all symbols
    max_same_direction: int      # open positions allowed to lean the same way across all symbols (0 = no cap)
    max_synthetic_positions: int # told to the model only; not checked in code
    min_stop_atr: float          # stop must be at least this many H1 ATRs from entry (0 = no check)
    max_risk_pct: float          # max % of balance per trade
    daily_loss_limit_pct: float          # circuit breaker
    max_total_risk_pct: float            # cap on risk across all open trades, % of balance
    blackout_utc: str                    # "HH:MM-HH:MM,..." no new forex/metal opens (UTC); "" disables
    max_latency_ms: float                # no new opens when broker ping exceeds this; 0 disables
    lockout_hours: float                 # stay out this long after the daily loss limit trips
    lockout_file: Path
    spread_max_pips: float               # don't trade if spread > this (forex/metals)
    synthetic_spread_max_pips: float     # spread cap for synthetic indices (much wider by nature)

    # Paths
    state_db: Path
    paper_db: Path
    kill_switch_file: Path
    prompt_file: Path

    # Auth (same as desk agents)
    claude_code_oauth_token: Optional[str] = None
    model_backend: str = "auto"

    @classmethod
    def from_env(cls, env_file: Optional[Path] = None) -> "Settings":
        load_dotenv(env_file or BASE_DIR / ".env", override=False)
        from agentcore.config import Env
        env = Env(PREFIX, BASE_DIR)
        _str, _int, _float, _bool = env.str, env.int, env.float, env.bool

        def _names(name: str, default: str) -> list[str]:
            raw = _str(name, default) or default
            return [s.strip() for s in raw.split(",") if s.strip()]

        return cls(
            mt5_login=_int("MT5_LOGIN", 0) or 0,
            mt5_password=_str("MT5_PASSWORD", "") or "",
            mt5_server=_str("MT5_SERVER", "Deriv-Demo") or "Deriv-Demo",
            anthropic_api_key=_str("ANTHROPIC_API_KEY"),
            model=_str("AGENT_MODEL", "claude-sonnet-4-6") or "claude-sonnet-4-6",
            max_agent_turns=_int("AGENT_MAX_TURNS", 16) or 16,
            max_tokens=_int("AGENT_MAX_TOKENS", 8192) or 8192,
            mode=(_str("MODE", "paper") or "paper").lower(),
            symbols=_names("SYMBOLS", DEFAULT_SYMBOLS),
            primary_timeframe=(_str("PRIMARY_TIMEFRAME", "H1") or "H1").upper(),
            confidence_threshold=_float("CONFIDENCE_THRESHOLD", 0.72) or 0.72,
            max_positions=_int("MAX_POSITIONS", 5) or 5,
            max_new_opens_per_scan=_int("MAX_NEW_OPENS_PER_SCAN", 3) or 3,
            max_same_direction=_int("MAX_SAME_DIRECTION", 3) or 0,
            max_synthetic_positions=_int("MAX_SYNTHETIC_POSITIONS", 3) or 3,
            min_stop_atr=_float("MIN_STOP_ATR", 1.0) or 0.0,
            max_risk_pct=_float("MAX_RISK_PCT", 3.0) or 3.0,
            daily_loss_limit_pct=_float("DAILY_LOSS_LIMIT_PCT", 5.0) or 5.0,
            max_total_risk_pct=_float("MAX_TOTAL_RISK_PCT", 40.0) or 40.0,
            blackout_utc=_str("BLACKOUT_UTC", "21:00-22:00") or "",
            max_latency_ms=_float("MAX_LATENCY_MS", 1000.0) or 0.0,
            lockout_hours=_float("LOCKOUT_HOURS", 24.0) or 24.0,
            lockout_file=BASE_DIR / "trading" / "state" / f"LOCKOUT.{(_str('MODE', 'paper') or 'paper').lower()}",
            spread_max_pips=_float("SPREAD_MAX_PIPS", 5.0) or 5.0,
            synthetic_spread_max_pips=_float("SYNTHETIC_SPREAD_MAX_PIPS", 10000.0) or 10000.0,
            state_db=BASE_DIR / "trading" / "state" / "agent.sqlite",
            paper_db=BASE_DIR / "trading" / "state" / "paper.sqlite",
            kill_switch_file=BASE_DIR / "trading" / "state" / "STOP",
            prompt_file=BASE_DIR / "trading" / "prompts" / "system_prompt.md",
            claude_code_oauth_token=_str("CLAUDE_CODE_OAUTH_TOKEN"),
            model_backend=(_str("MODEL_BACKEND", "auto") or "auto").lower(),
        )

    def problems(self) -> list[str]:
        from agentcore.sdk_backend import backend_problems
        errors: list[str] = []
        if self.mode not in VALID_MODES:
            errors.append(f"TRADING_MODE must be one of {VALID_MODES}, got {self.mode!r}")
        errors += backend_problems(self, need_model=True)
        if not self.mt5_login:
            errors.append("TRADING_MT5_LOGIN is required")
        if not self.mt5_password:
            errors.append("TRADING_MT5_PASSWORD is required")
        if not self.symbols:
            errors.append("TRADING_SYMBOLS is empty")
        if self.primary_timeframe not in VALID_TIMEFRAMES:
            errors.append(f"TRADING_PRIMARY_TIMEFRAME must be one of {VALID_TIMEFRAMES}")
        if not 0 < self.confidence_threshold <= 1:
            errors.append("TRADING_CONFIDENCE_THRESHOLD must be between 0 and 1")
        if not 0 < self.max_risk_pct <= 100:
            errors.append("TRADING_MAX_RISK_PCT must be between 0.1 and 100")
        from .breakers import parse_blackouts
        try:
            parse_blackouts(self.blackout_utc)
        except ValueError as exc:
            errors.append(f"TRADING_BLACKOUT_UTC: {exc}")
        from .guardrails import HARD_POSITION_CEILING
        if not 1 <= self.max_positions <= HARD_POSITION_CEILING:
            errors.append(f"TRADING_MAX_POSITIONS must be between 1 and {HARD_POSITION_CEILING}")
        if self.max_new_opens_per_scan < 1:
            errors.append("TRADING_MAX_NEW_OPENS_PER_SCAN must be at least 1")
        if self.min_stop_atr < 0:
            errors.append("TRADING_MIN_STOP_ATR must be 0 (no check) or more")
        if self.max_same_direction < 0:
            errors.append("TRADING_MAX_SAME_DIRECTION must be 0 (no cap) or more")
        if not self.prompt_file.exists():
            errors.append(f"Prompt file not found: {self.prompt_file}")
        return errors
