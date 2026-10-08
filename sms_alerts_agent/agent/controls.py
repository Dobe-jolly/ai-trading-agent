"""Operator controls shared by the agent and the portal: kill switch and mode override.

The portal can only make the agent safer: pause it, or hold it at a more restrictive mode
(live -> notes -> shadow). Going to live needs AGENT_MODE=live in the server's config.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

MODE_RANK = {"shadow": 0, "notes": 1, "live": 2}


def kill_switch_on(path: Path) -> bool:
    return path.exists()


def set_kill_switch(path: Path, on: bool, reason: str = "") -> None:
    if on:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(reason or "stopped\n", encoding="utf-8")
    elif path.exists():
        path.unlink()


def read_mode_override(path: Path) -> Optional[str]:
    try:
        value = path.read_text(encoding="utf-8").strip().lower()
    except (FileNotFoundError, OSError):
        return None
    return value if value in ("shadow", "notes") else None


def set_mode_override(path: Path, mode: Optional[str]) -> None:
    if mode is None:
        if path.exists():
            path.unlink()
        return
    if mode not in ("shadow", "notes"):
        raise ValueError("the override can only hold the agent at shadow or notes")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(mode + "\n", encoding="utf-8")
    os.replace(tmp, path)


def effective_mode(configured: str, override: Optional[str]) -> str:
    """The more restrictive of the configured mode and the override."""
    if override and MODE_RANK.get(override, 99) < MODE_RANK.get(configured, 99):
        return override
    return configured
