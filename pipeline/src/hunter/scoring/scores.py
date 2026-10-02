"""Scoring entry points.

The real V1 implementations live in :mod:`hunter.scoring.confidence` and
:mod:`hunter.scoring.free_score` (TASK-010 layout); this module re-exports them
for callers that import from ``hunter.scoring.scores``.
"""

from __future__ import annotations

from .confidence import verification_confidence
from .free_score import free_score

__all__ = ["verification_confidence", "free_score"]
