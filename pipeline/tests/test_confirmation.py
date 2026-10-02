"""TASK-010 tests: confirm_provider() — the exclusive FREE_CONFIRMED entry."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from hunter.evidence.models import Evidence, EvidenceProvenance, Officiality
from hunter.evidence.validator import OfficialEvidenceValidator, TrustAnchor
from hunter.llm.models import ExtractionResult, GroundedField
from hunter.registry.confirmation import (
    ConfirmationError,
    ConfirmationInput,
    confirm_provider,
)
from hunter.registry.schema import (
    FreeOffer,
    OfferKind,
    OfferStatus,
    Provider,
    ProviderApi,
    ProviderLimits,
    ProviderRequirements,
    ProviderStatus,
    ScoreMetadata,
)
from hunter.registry.store import ProviderRegistry

AS_OF = "2026-09-01T00:00:00+00:00"
AS_OF_DT = datetime.fromisoformat(AS_OF)


def _anchor() -> TrustAnchor:
    return TrustAnchor(
        provider_id="acme",
        domains=["acme.ai"],
        allow_subdomains=True,
        provenance="manual",
        reviewed_at="2026-09-01",
    )


def _evidence(official: Officiality = Officiality.OFFICIAL, source: str = "pricing") -> Evidence:
    provenance = None
    if official is Officiality.OFFICIAL:
        provenance = EvidenceProvenance(
            retrieval_method="safe_fetch",
            original_url="https://acme.ai/pricing",
            final_url="https://acme.ai/pricing",
            http_status=200,
            content_sha256="a" * 64,
            retrieved_from_origin=True,
            retrieved_at=datetime.fromisoformat("2026-08-01T00:00:00+00:00"),
        )
    return Evidence(
        evidence_id="ev-acme-pricing",
        provider_id="acme",
        url="https://acme.ai/pricing",
        source_type=source,
        officiality=official,
        provenance=provenance,
        retrieved_at=datetime.fromisoformat("2026-08-01T00:00:00+00:00"),
        effective_at=datetime.fromisoformat("2026-08-01T00:00:00+00:00"),
        claim="free plan programmatic API",
        content_excerpt="free plan programmatic API",
    )


def _extraction(ok: bool = True, offer_kind: str = "free_tier", **kw) -> ExtractionResult:
    quote = "free plan programmatic API"
    values = dict(kw)
    actual_kind = values.get("offer_kind", offer_kind)
    grounded_fields = values.pop("grounded_fields", None)
    if grounded_fields is None:
        grounded_fields = [
            GroundedField(
                field="offer_kind",
                value=actual_kind,
                evidence_id="ev-acme-pricing",
                quote=quote,
                start_offset=0,
                end_offset=len(quote),
            )
        ]
        for field_name in (
            "quota_mode",
            "renewal_period",
            "access_method",
            "description",
            "quota_text",
            "expires_at",
            "card_required",
            "phone_required",
            "commercial_use_allowed",
            "signup_required",
            "openai_compatible",
            "base_url",
        ):
            if values.get(field_name) is not None:
                value = values[field_name]
                grounded_fields.append(
                    GroundedField(
                        field=field_name,
                        value=value if field_name in {
                            "quota_mode",
                            "renewal_period",
                            "access_method",
                        } else None,
                        evidence_id="ev-acme-pricing",
                        quote=quote,
                        start_offset=0,
                        end_offset=len(quote),
                    )
                )
    return ExtractionResult(
        ok=ok,
        offer_kind=actual_kind,
        offer_status=None,
        grounded_fields=grounded_fields,
        **values,
    )


def _input(**kw) -> ConfirmationInput:
    defaults = dict(
        provider_name="Acme AI",
        canonical_domain="acme.ai",
        evidence=_evidence(),
        extraction=_extraction(),
        verification_confidence=90,
        free_score=70,
        as_of=AS_OF_DT,
        source_metadata={"pipeline": "confirm-cli"},
        reason="official pricing page documents free tier",
    )
    defaults.update(kw)
    return ConfirmationInput(**defaults)


def _registry(tmp_path: Path) -> ProviderRegistry:
    return ProviderRegistry(
        providers_path=tmp_path / "providers.json",
        history_path=tmp_path / "history.jsonl",
    )


def test_confirm_creates_free_confirmed_provider(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    validator = OfficialEvidenceValidator(anchors=[_anchor()])
    provider = confirm_provider(_input(), registry, validator)
    assert provider.status is ProviderStatus.FREE_CONFIRMED
    assert provider.verification_confidence == 90
    assert provider.free_score == 70
    assert provider.revision == 1
    assert provider.last_event_id
    assert provider.evidence_ids == ["ev-acme-pricing"]
    assert provider.free_offer.offer_status is OfferStatus.active
    assert provider.score_metadata.as_of == AS_OF_DT
    assert provider.score_metadata.score_version == "v1"
    assert provider.score_metadata.config_digest


def test_confirm_requires_official_evidence(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    validator = OfficialEvidenceValidator(anchors=[_anchor()])
    # evidence on an unanchored domain is at best LIKELY_OFFICIAL -> blocked
    ev = Evidence(
        evidence_id="ev-unauthored",
        provider_id="acme",
        url="https://free-list.example/pricing",
        source_type="pricing",
        officiality=Officiality.UNCONFIRMED,
        retrieved_at=datetime.fromisoformat("2026-08-01T00:00:00+00:00"),
        claim="free plan",
    )
    inp = _input(evidence=ev)
    with pytest.raises(ConfirmationError):
        confirm_provider(inp, registry, validator)


def test_confirm_requires_active_offer(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    validator = OfficialEvidenceValidator(anchors=[_anchor()])
    inp = _input(
        extraction=_extraction(
            ok=True,
            offer_kind="trial",
            access_method="api_key",
            expires_at="2026-08-10T00:00:00+00:00",  # before as_of -> expired
        )
    )
    # expiry is before as_of -> not active -> blocked
    with pytest.raises(ConfirmationError):
        confirm_provider(inp, registry, validator)


def test_confirm_requires_grounded_extraction(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    validator = OfficialEvidenceValidator(anchors=[_anchor()])
    inp = _input(extraction=_extraction(ok=False, failure_reason="ungrounded"))
    with pytest.raises(ConfirmationError):
        confirm_provider(inp, registry, validator)


def test_confirm_requires_confidence_threshold(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    validator = OfficialEvidenceValidator(anchors=[_anchor()])
    inp = _input(verification_confidence=70)
    with pytest.raises(ConfirmationError):
        confirm_provider(inp, registry, validator)


def test_confirm_promotion_requires_known_expiry(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    validator = OfficialEvidenceValidator(anchors=[_anchor()])
    inp = _input(extraction=_extraction(ok=True, offer_kind="promotion"))
    with pytest.raises(ConfirmationError):
        confirm_provider(inp, registry, validator)


def test_confirm_promotion_with_future_expiry_ok(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    validator = OfficialEvidenceValidator(anchors=[_anchor()])
    inp = _input(
        extraction=_extraction(
            ok=True,
            offer_kind="promotion",
            access_method="api_key",
            expires_at="2027-01-01T00:00:00+00:00",
        )
    )
    provider = confirm_provider(inp, registry, validator)
    assert provider.status is ProviderStatus.FREE_CONFIRMED


def test_confirm_blocks_on_unresolved_contradiction(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    validator = OfficialEvidenceValidator(anchors=[_anchor()])
    ev = _evidence()
    ev.validation_notes = ["contradiction: ev-a vs ev-b"]
    inp = _input(evidence=ev)
    with pytest.raises(ConfirmationError):
        confirm_provider(inp, registry, validator)


def test_confirm_normalizes_offer_fields(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    validator = OfficialEvidenceValidator(anchors=[_anchor()])
    inp = _input(
        extraction=_extraction(
            ok=True,
            offer_kind="trial",
            quota_mode="one_time",
            access_method="api_key",
            card_required=True,
            phone_required=False,
        )
    )
    provider = confirm_provider(inp, registry, validator)
    assert provider.free_offer.offer_kind is OfferKind.trial
    assert provider.free_offer.quota_mode.value == "one_time"
    assert provider.free_offer.access_method.value == "api_key"
    assert provider.requirements.card_required is True
    assert provider.requirements.phone_required is False


def test_confirm_generic_api_still_rejects_free_confirmed(tmp_path: Path) -> None:
    """The generic transition API remains unable to reach FREE_CONFIRMED."""
    from hunter.registry.state_machine import StateMachine, TransitionError

    registry = _registry(tmp_path)
    validator = OfficialEvidenceValidator(anchors=[_anchor()])
    provider = confirm_provider(_input(), registry, validator)
    machine = StateMachine(registry)
    with pytest.raises(TransitionError):
        machine.transition(
            provider,
            ProviderStatus.FREE_CONFIRMED,
            reason="attempt via generic API",
            source_metadata={"x": 1},
        )


def test_confirm_is_idempotent_no_new_history(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    validator = OfficialEvidenceValidator(anchors=[_anchor()])
    inp = _input()
    p1 = confirm_provider(inp, registry, validator)
    history_before = registry.history_path.read_text(encoding="utf-8") if registry.history_path.is_file() else ""
    p2 = confirm_provider(inp, registry, validator)
    history_after = registry.history_path.read_text(encoding="utf-8")
    assert history_before == history_after
    assert p2.revision == p1.revision == 1
    assert p2.last_event_id == p1.last_event_id


def test_confirm_persists_to_registry(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    validator = OfficialEvidenceValidator(anchors=[_anchor()])
    confirm_provider(_input(), registry, validator)
    assert len(registry.list_providers()) == 1
    reloaded = ProviderRegistry(
        providers_path=registry.providers_path,
        history_path=registry.history_path,
    )
    assert len(reloaded.list_providers()) == 1
