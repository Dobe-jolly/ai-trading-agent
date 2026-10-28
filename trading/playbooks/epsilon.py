"""Epsilon: Theta's z-score fade, applied to Step Index on M15.

Fixed rule, as approved in PLAYBOOKS.md (Phase 3). Only the market and timeframe differ from Theta.
"""
from __future__ import annotations

from . import register
from .theta import Theta


@register
class Epsilon(Theta):
    name = "epsilon"
    asset_classes = ("step",)
    timeframe = "M15"
