"""Structured extraction result models (TASK-009).

The model returns grounded fields; deterministic code derives the normalized
FreeOffer fields. offer_status is code-decided using an explicit as_of and is
never accepted from the model.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class GroundedField(BaseModel):
    """One field reference with its evidence quote and offsets."""

    model_config = ConfigDict(extra="ignore")

    field: str
    value: Optional[str] = None
    evidence_id: str
    quote: str
    start_offset: int
    end_offset: int


class ExtractionResult(BaseModel):
    """Deterministic outcome of a grounded extraction attempt."""

    model_config = ConfigDict(extra="forbid")

    ok: bool
    failure_reason: Optional[str] = None
    repairs_used: int = 0
    # normalized FreeOffer facts (all optional; null when unknown/ungrounded)
    offer_kind: Optional[str] = None
    quota_mode: Optional[str] = None
    renewal_period: Optional[str] = None
    access_method: Optional[str] = None
    offer_status: Optional[str] = None  # ALWAYS code-decided, never from model
    description: Optional[str] = None
    quota_text: Optional[str] = None
    expires_at: Optional[str] = None
    card_required: Optional[bool] = None
    phone_required: Optional[bool] = None
    commercial_use_allowed: Optional[bool] = None
    signup_required: Optional[bool] = None
    openai_compatible: Optional[bool] = None
    base_url: Optional[str] = None
    models: List[str] = Field(default_factory=list)
    grounded_fields: List[GroundedField] = Field(default_factory=list)
    raw_model_output: Dict[str, Any] = Field(default_factory=dict)
