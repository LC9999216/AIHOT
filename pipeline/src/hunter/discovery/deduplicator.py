"""Discovery deduplicator (TASK-005).

Exact observation-fingerprint deduplication that preserves the first-seen
observation and returns the duplicates separately. Distinct fingerprints are
always preserved — cross-source provenance is never dropped.
"""

from __future__ import annotations

from typing import List, Tuple

from .models import CandidateObservation


def deduplicate_observations(
    observations: List[CandidateObservation],
) -> Tuple[List[CandidateObservation], List[CandidateObservation]]:
    """Split into (unique, duplicates) by exact fingerprint, stable first-seen order."""
    unique: List[CandidateObservation] = []
    duplicates: List[CandidateObservation] = []
    seen: set = set()
    for obs in observations:
        if obs.fingerprint in seen:
            duplicates.append(obs)
        else:
            seen.add(obs.fingerprint)
            unique.append(obs)
    return unique, duplicates
