"""Compatibility shim.

The grounded extractor lives in :mod:`hunter.llm.structured_extractor` per the
TASK-009 layout; this module re-exports it so earlier imports keep working.
"""

from __future__ import annotations

from .structured_extractor import (
    ExtractionError,
    GroundedExtractor,
    validate_grounding,
)

__all__ = ["GroundedExtractor", "ExtractionError", "validate_grounding"]
