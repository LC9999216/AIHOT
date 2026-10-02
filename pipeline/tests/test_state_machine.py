"""TASK-004 tests: Provider State Machine.

The generic transition API must enforce the AGENTS.md ordinary transition
graph and must NEVER accept FREE_CONFIRMED as a destination. Only
confirm_provider() (TASK-010) may reach FREE_CONFIRMED.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hunter.registry.schema import (
    FreeOffer,
    OfferKind,
    OfferStatus,
    Provider,
    ProviderStatus,
)
from hunter.registry.state_machine import (
    StateMachine,
    TransitionError,
    allowed_transitions,
)
from hunter.registry.store import ProviderRegistry

ALL_STATES = list(ProviderStatus)


def _provider(**overrides) -> Provider:
    base = dict(
        id="provider-1",
        provider="Acme AI",
        canonical_domain="acme.ai",
        status=ProviderStatus.DISCOVERED,
        free_offer=FreeOffer(offer_kind="free_tier"),
    )
    base.update(overrides)
    if base.get("status") is ProviderStatus.FREE_CONFIRMED:
        base.setdefault("evidence_ids", ["ev-1"])
        base.setdefault("verification_confidence", 90)
        base.setdefault("free_score", 80)
        from hunter.registry.schema import ScoreMetadata

        base.setdefault(
            "score_metadata",
            ScoreMetadata(score=90, as_of="2026-09-01T00:00:00+00:00"),
        )
    return Provider(**base)


def _machine(tmp_path: Path) -> tuple[StateMachine, ProviderRegistry]:
    reg = ProviderRegistry(
        providers_path=tmp_path / "providers.json",
        history_path=tmp_path / "history.jsonl",
    )
    return StateMachine(reg), reg


# --- transition graph -------------------------------------------------------


def test_allowed_transitions_table() -> None:
    expected = {
        ProviderStatus.DISCOVERED: {ProviderStatus.EVIDENCE_PENDING},
        ProviderStatus.EVIDENCE_PENDING: {
            ProviderStatus.EVIDENCE_VERIFIED,
            ProviderStatus.UNCERTAIN,
            ProviderStatus.NOT_FREE,
            ProviderStatus.REJECTED,
        },
        ProviderStatus.EVIDENCE_VERIFIED: {
            ProviderStatus.UNCERTAIN,
            ProviderStatus.NOT_FREE,
        },
        ProviderStatus.FREE_CONFIRMED: {ProviderStatus.EXPIRED, ProviderStatus.UNCERTAIN},
        ProviderStatus.UNCERTAIN: {ProviderStatus.EVIDENCE_PENDING},
        ProviderStatus.EXPIRED: {ProviderStatus.EVIDENCE_PENDING},
        ProviderStatus.NOT_FREE: {ProviderStatus.EVIDENCE_PENDING},
        ProviderStatus.REJECTED: set(),
    }
    assert allowed_transitions() == expected


@pytest.mark.parametrize(
    "current,new_state",
    [
        (ProviderStatus.DISCOVERED, ProviderStatus.EVIDENCE_PENDING),
        (ProviderStatus.EVIDENCE_PENDING, ProviderStatus.EVIDENCE_VERIFIED),
        (ProviderStatus.EVIDENCE_PENDING, ProviderStatus.UNCERTAIN),
        (ProviderStatus.EVIDENCE_PENDING, ProviderStatus.NOT_FREE),
        (ProviderStatus.EVIDENCE_PENDING, ProviderStatus.REJECTED),
        (ProviderStatus.EVIDENCE_VERIFIED, ProviderStatus.UNCERTAIN),
        (ProviderStatus.EVIDENCE_VERIFIED, ProviderStatus.NOT_FREE),
        (ProviderStatus.FREE_CONFIRMED, ProviderStatus.EXPIRED),
        (ProviderStatus.FREE_CONFIRMED, ProviderStatus.UNCERTAIN),
        (ProviderStatus.UNCERTAIN, ProviderStatus.EVIDENCE_PENDING),
        (ProviderStatus.EXPIRED, ProviderStatus.EVIDENCE_PENDING),
        (ProviderStatus.NOT_FREE, ProviderStatus.EVIDENCE_PENDING),
    ],
)
def test_every_ordinary_allowed_transition(tmp_path: Path, current, new_state) -> None:
    sm, reg = _machine(tmp_path)
    provider = _provider(status=current)
    reg.upsert_provider(provider, reason="setup", source_metadata={})
    result = sm.transition(
        reg.get_provider("provider-1"),
        new_state,
        reason=f"{current.value}->{new_state.value}",
        source_metadata={"src": "test"},
    )
    assert result.status is new_state
    got = reg.get_provider("provider-1")
    assert got.status is new_state
    assert got.revision == 2  # setup(1) + transition(2)


def test_representative_forbidden_transitions(tmp_path: Path) -> None:
    forbidden = [
        (ProviderStatus.DISCOVERED, ProviderStatus.EVIDENCE_VERIFIED),
        (ProviderStatus.DISCOVERED, ProviderStatus.UNCERTAIN),
        (ProviderStatus.EVIDENCE_VERIFIED, ProviderStatus.EVIDENCE_PENDING),
        (ProviderStatus.REJECTED, ProviderStatus.EVIDENCE_PENDING),
        (ProviderStatus.UNCERTAIN, ProviderStatus.NOT_FREE),
        (ProviderStatus.EVIDENCE_PENDING, ProviderStatus.EVIDENCE_PENDING),  # self
    ]
    for i, (current, new_state) in enumerate(forbidden):
        sm, reg = _machine(tmp_path / f"case-{i}")
        reg.upsert_provider(_provider(status=current), reason="setup", source_metadata={})
        before = reg.get_provider("provider-1")
        with pytest.raises(TransitionError):
            sm.transition(before, new_state, reason="x", source_metadata={})
        after = reg.get_provider("provider-1")
        assert after.status is current
        assert after.revision == before.revision
        assert len(reg.history._lines()) == 1  # only the setup event


# --- FREE_CONFIRMED guards --------------------------------------------------


def test_generic_api_rejects_free_confirmed_from_evidence_verified(tmp_path: Path) -> None:
    sm, reg = _machine(tmp_path)
    reg.upsert_provider(
        _provider(status=ProviderStatus.EVIDENCE_VERIFIED), reason="setup", source_metadata={}
    )
    before = reg.get_provider("provider-1")
    with pytest.raises(TransitionError) as exc:
        sm.transition(before, ProviderStatus.FREE_CONFIRMED, reason="x", source_metadata={})
    assert "FREE_CONFIRMED" in str(exc.value)
    assert reg.get_provider("provider-1").status is ProviderStatus.EVIDENCE_VERIFIED
    assert reg.get_provider("provider-1").revision == before.revision


@pytest.mark.parametrize("current", ALL_STATES)
def test_generic_api_rejects_free_confirmed_from_every_state(tmp_path: Path, current) -> None:
    sm, reg = _machine(tmp_path)
    reg.upsert_provider(_provider(status=current), reason="setup", source_metadata={})
    with pytest.raises(TransitionError):
        sm.transition(
            reg.get_provider("provider-1"), ProviderStatus.FREE_CONFIRMED, reason="x", source_metadata={}
        )


def test_no_direct_public_field_mutation_to_free_confirmed(tmp_path: Path) -> None:
    sm, reg = _machine(tmp_path)
    reg.upsert_provider(_provider(), reason="setup", source_metadata={})
    # The schema gate forbids FREE_CONFIRMED records without scores/evidence
    # even if someone tried to build one directly (no scores/evidence here).
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Provider(
            id="provider-2",
            provider="Acme AI",
            status=ProviderStatus.FREE_CONFIRMED,
            free_offer=FreeOffer(offer_kind="free_tier", offer_status="active"),
        )
    assert reg.get_provider("provider-1").status is ProviderStatus.DISCOVERED
    # And the generic transition API refuses FREE_CONFIRMED for a valid record.
    with pytest.raises(TransitionError):
        sm.transition(
            reg.get_provider("provider-1"), ProviderStatus.FREE_CONFIRMED, reason="x", source_metadata={}
        )
    assert reg.get_provider("provider-1").status is ProviderStatus.DISCOVERED


# --- requirements & history -------------------------------------------------


def test_transition_requires_reason(tmp_path: Path) -> None:
    sm, reg = _machine(tmp_path)
    reg.upsert_provider(_provider(), reason="setup", source_metadata={})
    with pytest.raises(TransitionError):
        sm.transition(reg.get_provider("provider-1"), ProviderStatus.EVIDENCE_PENDING, reason="", source_metadata={})


def test_transition_requires_source_metadata(tmp_path: Path) -> None:
    sm, reg = _machine(tmp_path)
    reg.upsert_provider(_provider(), reason="setup", source_metadata={})
    with pytest.raises(TransitionError):
        sm.transition(reg.get_provider("provider-1"), ProviderStatus.EVIDENCE_PENDING, reason="r", source_metadata=None)


def test_transition_creates_history_event(tmp_path: Path) -> None:
    sm, reg = _machine(tmp_path)
    reg.upsert_provider(_provider(), reason="setup", source_metadata={})
    sm.transition(
        reg.get_provider("provider-1"),
        ProviderStatus.EVIDENCE_PENDING,
        reason="found evidence",
        source_metadata={"src": "github"},
    )
    events = [json.loads(line) for line in reg.history._lines()]
    assert len(events) == 2
    transition_event = events[1]
    assert transition_event["event_type"] == "provider.transition"
    assert transition_event["reason"] == "found evidence"
    assert "status" in transition_event["changed_fields"]


def test_recovery_transitions_return_to_evidence_pending(tmp_path: Path) -> None:
    sm, reg = _machine(tmp_path)
    for current in (ProviderStatus.EXPIRED, ProviderStatus.NOT_FREE):
        reg.upsert_provider(_provider(status=current), reason="setup", source_metadata={})
        result = sm.transition(
            reg.get_provider("provider-1"), ProviderStatus.EVIDENCE_PENDING, reason="recheck", source_metadata={}
        )
        assert result.status is ProviderStatus.EVIDENCE_PENDING


def test_failure_atomicity(tmp_path: Path) -> None:
    """An illegal transition leaves registry AND history untouched."""
    sm, reg = _machine(tmp_path)
    reg.upsert_provider(_provider(), reason="setup", source_metadata={})
    registry_before = reg.providers_path.read_bytes()
    history_before = reg.history_path.read_bytes() if reg.history_path.exists() else b""
    with pytest.raises(TransitionError):
        sm.transition(reg.get_provider("provider-1"), ProviderStatus.FREE_CONFIRMED, reason="x", source_metadata={})
    assert reg.providers_path.read_bytes() == registry_before
    assert reg.history_path.read_bytes() == history_before
