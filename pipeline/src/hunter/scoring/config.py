"""Scoring configuration and result metadata (TASK-010).

Weights live in ``config/scoring.yaml`` and are fixed by the specification.
A configuration digest is derived from the file content for every result.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..discovery.models import _require_aware_iso


class ScoreConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    score_version: str = "v1"
    verification_threshold: int = 80
    verification: Dict[str, Any] = Field(default_factory=dict)
    free_score: Dict[str, Any] = Field(default_factory=dict)
    config_digest: str = ""


class ScoreMetadata(BaseModel):
    """Result envelope stored on a Provider (AGENTS.md 4.4 / 11)."""

    model_config = ConfigDict(extra="forbid")

    score: int
    score_version: str = "v1"
    as_of: datetime
    config_digest: str = ""
    breakdown: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("as_of")
    @classmethod
    def _as_of(cls, v: datetime) -> datetime:
        return _require_aware_iso(v, "as_of")


def load_scoring_config(path: Path) -> ScoreConfig:
    """Load config/scoring.yaml and derive its digest."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    digest = hashlib.sha256(json.dumps(raw, sort_keys=True).encode("utf-8")).hexdigest()[:40]
    verification = raw.get("verification_confidence") or {}
    threshold = verification.get("threshold", 80)
    return ScoreConfig(
        score_version=raw.get("score_version", "v1"),
        verification_threshold=int(threshold),
        verification=verification,
        free_score=raw.get("free_score") or {},
        config_digest=digest,
    )
