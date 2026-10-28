"""Registry of playbooks that can be tested. Empty until a phase adds its playbooks."""
from __future__ import annotations

from .base import Bars, Playbook, Signal

REGISTRY: dict[str, type[Playbook]] = {}


def register(cls: type[Playbook]) -> type[Playbook]:
    if not cls.name:
        raise ValueError(f"{cls.__name__} has no name")
    if cls.name in REGISTRY:
        raise ValueError(f"playbook {cls.name!r} is registered twice")
    REGISTRY[cls.name] = cls
    return cls


# Importing the modules registers their playbooks. Kept at the bottom because they import `register`.
from . import alpha, beta, gamma, theta  # noqa: E402,F401

__all__ = ["Bars", "Playbook", "Signal", "REGISTRY", "register"]
