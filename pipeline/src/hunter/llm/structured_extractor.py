"""Grounded structured extractor (TASK-009; AGENTS.md 10).

Deterministic validation is the authority: every accepted non-null field must
carry an ``evidence_id``, a ``quote``, and offsets that select exactly that
quote from the supplied evidence text. Schema-valid but ungrounded values are
rejected. Malformed output receives at most one repair attempt; failure is
recorded and the Candidate stays retryable. The extractor never mutates
Provider state and never decides officiality, scores, or offer status.
"""

from __future__ import annotations

import inspect
import json
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..evidence.models import Evidence
from .client import LlmRequest, assert_no_secrets, assert_no_tools
from .models import ExtractionResult, GroundedField
from .prompts import (
    EXTRACTION_SCHEMA,
    FORBIDDEN_MODEL_FIELDS,
    SCALAR_ENUM_FIELDS,
    SUPPORTED_FIELDS,
    SYSTEM_PROMPT,
    build_extraction_prompt,
    build_repair_prompt,
)

_VALID_ENUMS = {
    "offer_kind": {"free_tier", "free_credit", "trial", "promotion", "unknown"},
    "quota_mode": {"unmetered", "renewing", "one_time", "unknown"},
    "renewal_period": {"daily", "weekly", "monthly", "custom"},
    "access_method": {"api_key", "keyless", "oauth", "unknown"},
}

_TRUTHY = ("true", "yes", "required", "1")
_FALSY = ("false", "no", "not required", "0")


class ExtractionError(Exception):
    """Raised for structural extraction failures (bad output after repairs)."""


def _quote_matches(text: str, quote: str, start: int, end: int) -> bool:
    if start < 0 or end < 0 or end <= start:
        return False
    if end > len(text):
        return False
    return text[start:end] == quote


def validate_grounding(
    fields: Sequence[Any], evidences: Sequence[Evidence]
) -> List[str]:
    """Return a list of grounding errors for grounded fields."""
    errors: List[str] = []
    by_id = {e.evidence_id: e for e in evidences}
    for item in fields:
        f = item if isinstance(item, GroundedField) else GroundedField.model_validate(item)
        if not f.field:
            errors.append("missing field name")
            continue
        if f.field not in SUPPORTED_FIELDS:
            errors.append(f"unsupported field {f.field!r}")
            continue
        if f.field in _VALID_ENUMS and f.value not in _VALID_ENUMS[f.field]:
            errors.append(f"unknown enum value {f.value!r} for {f.field!r}")
            continue
        evidence = by_id.get(f.evidence_id)
        if evidence is None:
            errors.append(f"unknown evidence_id {f.evidence_id!r}")
            continue
        text = evidence.content_excerpt or ""
        if not _quote_matches(text, f.quote, f.start_offset, f.end_offset):
            errors.append(
                f"field {f.field!r} quote/offsets do not match evidence {f.evidence_id}"
            )
    return errors


class GroundedExtractor:
    """Runs the tool-less LLM boundary and validates grounding deterministically."""

    def __init__(
        self,
        llm: Any,
        max_repairs: int = 1,
        secret_values: Optional[Sequence[str]] = None,
    ):
        self.llm = llm
        self.max_repairs = max_repairs
        self.secret_values = list(secret_values or [])

    def extract(self, evidence: Evidence, as_of: str) -> ExtractionResult:
        text = evidence.content_excerpt or ""
        prompt = build_extraction_prompt(evidence.evidence_id, text)
        attempt = 0
        while True:
            raw_text = self._invoke(prompt, evidence, text)
            parsed, error = self._parse(raw_text)
            if parsed is None:
                if attempt < self.max_repairs:
                    prompt = build_repair_prompt(
                        evidence.evidence_id, text, f"malformed JSON: {error}"
                    )
                    attempt += 1
                    continue
                return ExtractionResult(
                    ok=False,
                    failure_reason=f"malformed model output after repairs: {error}",
                    repairs_used=attempt,
                )
            fields, verbatim, field_errors = self._to_grounded(parsed)
            errors = field_errors + validate_grounding(fields, [evidence])
            if errors:
                if attempt < self.max_repairs:
                    prompt = build_repair_prompt(evidence.evidence_id, text, "; ".join(errors))
                    attempt += 1
                    continue
                return ExtractionResult(
                    ok=False,
                    failure_reason="ungrounded model output after repairs: " + "; ".join(errors),
                    repairs_used=attempt,
                    grounded_fields=fields,
                    raw_model_output=parsed,
                )
            return self._normalize(parsed, fields, verbatim, attempt)

    # --- client invocation --------------------------------------------------

    def _invoke(self, prompt: str, evidence: Evidence, text: str) -> str:
        """Call the injected client through either supported interface."""
        assert_no_secrets(prompt, self.secret_values)
        request = LlmRequest(
            system=SYSTEM_PROMPT,
            user=prompt,
            schema=EXTRACTION_SCHEMA,
            evidence_id=evidence.evidence_id,
            evidence_text=text,
            tools=None,
        )
        assert_no_tools(request)
        params = list(inspect.signature(self.llm.complete).parameters)
        if params and params[0] in ("request", "req"):
            response = self.llm.complete(request)
            return getattr(response, "text", response) if not isinstance(response, str) else response
        result = self.llm.complete(prompt, SYSTEM_PROMPT)
        return result if isinstance(result, str) else getattr(result, "text", "")

    # --- parsing / grounding ------------------------------------------------

    def _parse(self, raw: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`")
            if cleaned.startswith("json"):
                cleaned = cleaned[4:]
        try:
            parsed = json.loads(cleaned)
        except (json.JSONDecodeError, ValueError) as exc:
            return None, str(exc)
        if not isinstance(parsed, dict):
            return None, "output is not a JSON object"
        return parsed, None

    def _to_grounded(
        self, parsed: Dict[str, Any]
    ) -> Tuple[List[GroundedField], Dict[str, Any], List[str]]:
        fields: List[GroundedField] = []
        verbatim: Dict[str, Any] = {}
        errors: List[str] = []
        for name, value in parsed.items():
            if name in FORBIDDEN_MODEL_FIELDS:
                # authority the model does not have: ignored, never applied
                continue
            if name not in SUPPORTED_FIELDS:
                continue  # extra fields are dropped (null), never accepted
            if name == "models":
                if not isinstance(value, list):
                    errors.append("models must be a list")
                    continue
                for item in value:
                    gf, errs = self._field_from(item, name)
                    fields.extend(gf)
                    errors.extend(errs)
                continue
            if name in SCALAR_ENUM_FIELDS:
                if not isinstance(value, dict):
                    errors.append(f"{name}: expected grounded object with value")
                    continue
                enum_value = value.get("value")
                if enum_value not in _VALID_ENUMS.get(name, set()):
                    errors.append(f"{name}: unknown enum value {enum_value!r}")
                    continue
                gf, errs = self._field_from(value, name, enum_value=enum_value)
                fields.extend(gf)
                errors.extend(errs)
                if not errs:
                    verbatim[name] = enum_value
                continue
            gf, errs = self._field_from(value, name)
            fields.extend(gf)
            errors.extend(errs)
        return fields, verbatim, errors

    def _field_from(
        self, value: Any, name: str, enum_value: Optional[str] = None
    ) -> Tuple[List[GroundedField], List[str]]:
        if value is None:
            return [], []
        if not isinstance(value, dict):
            return [], [f"{name}: expected grounded object"]
        quote = value.get("quote")
        start = value.get("start_offset")
        end = value.get("end_offset")
        evidence_id = value.get("evidence_id")
        if not isinstance(quote, str) or not isinstance(start, int) or not isinstance(end, int):
            return [], [f"{name}: missing quote or offsets"]
        gf = GroundedField(
            field=name,
            value=enum_value,
            evidence_id=str(evidence_id or ""),
            quote=quote,
            start_offset=start,
            end_offset=end,
        )
        return [gf], []

    # --- normalization -------------------------------------------------------

    def _normalize(
        self,
        parsed: Dict[str, Any],
        fields: List[GroundedField],
        verbatim: Dict[str, Any],
        repairs: int,
    ) -> ExtractionResult:
        grounded: Dict[str, str] = {}
        models: List[str] = []
        for f in fields:
            if f.field == "models":
                models.append(f.quote)
            elif f.field in SCALAR_ENUM_FIELDS:
                grounded[f.field] = f.value or ""
            else:
                grounded[f.field] = f.quote
        return ExtractionResult(
            ok=True,
            repairs_used=repairs,
            offer_kind=verbatim.get("offer_kind") or grounded.get("offer_kind"),
            quota_mode=verbatim.get("quota_mode") or grounded.get("quota_mode"),
            renewal_period=verbatim.get("renewal_period") or grounded.get("renewal_period"),
            access_method=verbatim.get("access_method") or grounded.get("access_method"),
            offer_status=None,  # code-decided only; never taken from the model
            description=grounded.get("description"),
            quota_text=grounded.get("quota_text"),
            expires_at=grounded.get("expires_at"),
            card_required=_bool_or_none(grounded.get("card_required")),
            phone_required=_bool_or_none(grounded.get("phone_required")),
            commercial_use_allowed=_bool_or_none(grounded.get("commercial_use_allowed")),
            signup_required=_bool_or_none(grounded.get("signup_required")),
            openai_compatible=_bool_or_none(grounded.get("openai_compatible")),
            base_url=grounded.get("base_url"),
            models=models,
            grounded_fields=fields,
            raw_model_output=parsed,
        )


def _bool_or_none(value: Optional[str]) -> Optional[bool]:
    if value is None:
        return None
    lowered = value.strip().lower()
    if lowered in _TRUTHY:
        return True
    if lowered in _FALSY:
        return False
    return None


__all__ = ["GroundedExtractor", "ExtractionError", "validate_grounding"]
