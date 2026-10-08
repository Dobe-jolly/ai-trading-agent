"""The desks the service runs, in the order a pass visits them.

Each desk is a package (sms_alerts, ecocash, ...) with its own settings (prefix <PREFIX>_), decision log and
console views, built on agentcore. DESKS in the .env picks which run and in what order.
"""
from __future__ import annotations

import importlib
import os
from pathlib import Path
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any, Optional

from agentcore.config import ROOT


@dataclass(frozen=True)
class Desk:
    key: str                  # package name and --desk value
    title: str                # what people call it
    prefix: str               # settings prefix
    commands: tuple = ()      # commands only this desk has
    blurb: str = ""
    extra: dict = field(default_factory=dict)

    def module(self, name: str = "") -> ModuleType:
        return importlib.import_module(self.key + (f".{name}" if name else ""))

    def settings(self, env_file: Optional[str] = None) -> Any:
        return self.module("config").Settings.from_env(env_file)

    def main(self, argv: list[str]) -> int:
        return self.module("__main__").main(argv)


ALL = {
    "sms_alerts": Desk("sms_alerts", "SMS Alerts", "SMS_ALERTS", ("check-account",),
                       "Clients not receiving SMS alerts: checks registration, the CAE request and the gateway."),
    "ecocash": Desk("ecocash", "EcoCash", "ECOCASH", ("lookup", "monitor"),
                    "EcoCash account linking: reads the link attempts the monitor records and EcoCash's answer."),
    "failed_txn": Desk("failed_txn", "Failed transactions", "FAILED_TXN", ("find-transactions",),
                       "ZIPIT, B2W/W2B, card, NMB Connect, airtime and bill payments: the switch, the billers and "
                       "core banking."),
}
DEFAULT_ORDER = "sms_alerts,ecocash,failed_txn"


def enabled() -> list[Desk]:
    """The desks named in DESKS (default: all), in that order."""
    names = [x.strip().lower() for x in (os.getenv("DESKS") or DEFAULT_ORDER).split(",") if x.strip()]
    unknown = [n for n in names if n not in ALL]
    if unknown:
        raise ValueError(f"DESKS names unknown desk(s) {', '.join(unknown)}; known: {', '.join(ALL)}")
    return [ALL[n] for n in dict.fromkeys(names)]


def get(key: str) -> Desk:
    try:
        return ALL[key.strip().lower().replace("-", "_")]
    except KeyError:
        raise ValueError(f"unknown desk {key!r}; known: {', '.join(ALL)}") from None


def service_stop_file() -> Path:
    """Pauses every desk (each desk also has its own, state/<desk>/STOP)."""
    value = os.getenv("SERVICE_KILL_SWITCH_FILE") or "state/STOP"
    path = Path(value)
    return path if path.is_absolute() else ROOT / path
