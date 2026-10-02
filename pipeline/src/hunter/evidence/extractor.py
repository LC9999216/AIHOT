"""Evidence-layer extraction facade (TASK-009).

Keeps the evidence package's public entry point thin: it delegates to the
grounded structured extractor and returns the deterministic result. This layer
holds no LLM authority — it cannot set officiality, scores, or Provider state.
"""

from __future__ import annotations

from typing import Any, List, Optional, Sequence

from ..llm.models import ExtractionResult
from ..llm.structured_extractor import (
    ExtractionError,
    GroundedExtractor,
    validate_grounding,
)
from .models import Evidence


def extract_evidence(
    evidence: Evidence,
    as_of: str,
    llm: Any,
    max_repairs: int = 1,
    secret_values: Optional[Sequence[str]] = None,
) -> ExtractionResult:
    """Run grounded extraction for one evidence item."""
    extractor = GroundedExtractor(
        llm=llm, max_repairs=max_repairs, secret_values=secret_values
    )
    return extractor.extract(evidence, as_of)


__all__ = [
    "extract_evidence",
    "GroundedExtractor",
    "ExtractionError",
    "validate_grounding",
]
