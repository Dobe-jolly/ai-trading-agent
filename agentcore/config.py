"""Reading settings from environment variables (and the service's .env file).

All desks run in one service with one .env. A setting can be given once for every desk (AGENT_MODE=shadow)
or for one desk with the desk's prefix (ECOCASH_AGENT_MODE=live), which wins for that desk.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent


class Env:
    def __init__(self, prefix: Optional[str] = None, base_dir: Path = ROOT) -> None:
        self.prefix = (prefix or "").strip().upper().rstrip("_") or None
        self.base_dir = base_dir

    def key(self, name: str) -> str:
        """The variable that is actually set for this name (the desk's own first), for messages."""
        if self.prefix and _value(f"{self.prefix}_{name}") is not None:
            return f"{self.prefix}_{name}"
        return name

    def str(self, name: str, default: Optional[str] = None) -> Optional[str]:
        if self.prefix:
            value = _value(f"{self.prefix}_{name}")
            if value is not None:
                return value
        value = _value(name)
        return default if value is None else value

    def int(self, name: str, default: Optional[int]) -> Optional[int]:
        value = self.str(name)
        if value is None:
            return default
        try:
            return int(value)
        except ValueError:
            raise ValueError(f"{self.key(name)} must be a whole number, got {value!r}") from None

    def float(self, name: str, default: float) -> float:
        value = self.str(name)
        try:
            return float(value) if value is not None else default
        except ValueError:
            raise ValueError(f"{self.key(name)} must be a number, got {value!r}") from None

    def bool(self, name: str, default: bool) -> bool:
        value = self.str(name)
        if value is None:
            return default
        return value.lower() in ("true", "1", "yes", "on")

    def names(self, name: str, default: str) -> tuple:
        """Comma-separated names (help topics may contain spaces and slashes)."""
        return tuple(x.strip() for x in (self.str(name, default) or "").split(",") if x.strip())

    def path(self, name: str, default: str) -> Path:
        path = Path(self.str(name, default) or default)
        return path if path.is_absolute() else self.base_dir / path


def _value(name: str) -> Optional[str]:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return None
    return value.strip()
