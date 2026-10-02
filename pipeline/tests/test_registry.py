"""TASK-003 tests: Provider Registry Store and recoverable History."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hunter.registry.history import History, HistoryError
from hunter.registry.schema import FreeOffer, Provider, ProviderStatus
from hunter.registry.store import (
    ProviderRegistry,
    RegistryError,
    make_event_id,
)


def _provider(**overrides) -> Provider:
    base = dict(
        id="provider-1",
        provider="Acme AI",
        canonical_domain="acme.ai",
        status=ProviderStatus.DISCOVERED,
        free_offer=FreeOffer(offer_kind="free_tier"),
    )
    base.update(overrides)
    return Provider(**base)


def _registry(tmp_path: Path) -> ProviderRegistry:
    return ProviderRegistry(
        providers_path=tmp_path / "providers.json",
        history_path=tmp_path / "history.jsonl",
    )


def _provider_copy_with_changed_name(p: Provider, name: str) -> Provider:
    return p.model_copy(update={"provider": name})


# --- list / get / find ------------------------------------------------------


def test_empty_registry(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    assert reg.list_providers() == []
    assert reg.get_provider("missing") is None
    assert reg.find_by_domain("acme.ai") is None


def test_create_provider(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    p = _provider()
    reg.upsert_provider(p, reason="discovered", source_metadata={"source": "seed"})
    got = reg.get_provider("provider-1")
    assert got is not None
    assert got.revision == 1
    assert got.last_event_id is not None
    assert reg.list_providers() == [got]


def test_find_by_domain_exact(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    reg.upsert_provider(_provider(), reason="r", source_metadata={})
    found = reg.find_by_domain("acme.ai")
    assert found is not None
    assert found.id == "provider-1"


def test_find_by_domain_normalized(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    reg.upsert_provider(_provider(canonical_domain="ACME.AI."), reason="r", source_metadata={})
    found = reg.find_by_domain("acme.ai")
    assert found is not None


def test_lookup_by_alias(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    reg.upsert_provider(_provider(), reason="r", source_metadata={})
    # alias lookup via metadata aliases configured in store
    reg._aliases["acme-alias"] = "provider-1"
    found = reg.find_by_domain_or_alias("acme-alias")
    assert found is not None


def test_lookup_by_unambiguous_exact_name(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    reg.upsert_provider(_provider(), reason="r", source_metadata={})
    found = reg.find_by_name("Acme AI")
    assert found is not None
    assert found.id == "provider-1"


def test_ambiguous_name_returns_none(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    reg.upsert_provider(_provider(), reason="r", source_metadata={})
    reg.upsert_provider(
        _provider(id="provider-2", canonical_domain="acme2.ai"), reason="r", source_metadata={}
    )
    # both now share the name "Acme AI" (provider-1 and provider-2)
    found = reg.find_by_name("Acme AI")
    assert found is None


# --- updates, no-op, revision ----------------------------------------------


def test_update_increments_revision(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    reg.upsert_provider(_provider(), reason="r1", source_metadata={})
    updated = _provider_copy_with_changed_name(reg.get_provider("provider-1"), "Acme AI Pro")
    reg.upsert_provider(updated, reason="r2", source_metadata={})
    got = reg.get_provider("provider-1")
    assert got.revision == 2
    assert got.provider == "Acme AI Pro"


def test_noop_update_does_nothing(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    reg.upsert_provider(_provider(), reason="r1", source_metadata={})
    providers_bytes = reg.providers_path.read_bytes()
    history_lines = len(reg.history._lines())
    # re-upsert the same semantic content
    reg.upsert_provider(_provider(), reason="r1", source_metadata={})
    assert reg.providers_path.read_bytes() == providers_bytes
    assert len(reg.history._lines()) == history_lines
    assert reg.get_provider("provider-1").revision == 1


def test_save_and_reload(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    reg.upsert_provider(_provider(), reason="r1", source_metadata={})
    reg2 = _registry(tmp_path)
    got = reg2.get_provider("provider-1")
    assert got is not None
    assert got.revision == 1


def test_serialized_stable_id_order(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    reg.upsert_provider(_provider(id="z-provider"), reason="r", source_metadata={})
    reg.upsert_provider(_provider(id="a-provider"), reason="r", source_metadata={})
    ids = [p.id for p in reg.list_providers()]
    assert ids == sorted(ids)


def test_invalid_provider_record_rejected(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    # Not a Provider instance at all.
    with pytest.raises(RegistryError):
        reg.upsert_provider("not-a-provider", reason="r", source_metadata={})
    # A Provider that fails store-side validation (invalid score) fails closed.
    from pydantic import ValidationError

    with pytest.raises((RegistryError, ValidationError)):
        reg.upsert_provider(_provider(verification_confidence=500), reason="r", source_metadata={})
    assert reg.get_provider("provider-1") is None


# --- history -----------------------------------------------------------------


def test_history_event_fields(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    reg.upsert_provider(
        _provider(), reason="discovered via seed", source_metadata={"source": "seed"}
    )
    lines = reg.history._lines()
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["provider_id"] == "provider-1"
    assert event["revision"] == 1
    assert event["event_type"] == "provider.created"
    assert event["changed_fields"]
    assert event["reason"] == "discovered via seed"
    assert "timestamp" in event
    assert "event_id" in event


def test_update_event_type_is_upsert(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    reg.upsert_provider(_provider(), reason="r1", source_metadata={})
    reg.upsert_provider(
        _provider_copy_with_changed_name(reg.get_provider("provider-1"), "Acme AI Pro"),
        reason="r2",
        source_metadata={},
    )
    events = [json.loads(line) for line in reg.history._lines()]
    assert events[0]["event_type"] == "provider.created"
    assert events[1]["event_type"] == "provider.upsert"


def test_history_event_id_deterministic(tmp_path: Path) -> None:
    ev1 = make_event_id("p", 1, "abc")
    ev2 = make_event_id("p", 1, "abc")
    assert ev1 == ev2
    assert make_event_id("p", 2, "abc") != ev1
    assert make_event_id("p", 1, "abd") != ev1


def test_history_no_duplicate_events(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    reg.upsert_provider(_provider(), reason="r", source_metadata={})
    n1 = len(reg.history._lines())
    # Replay the same event through append; idempotent by event_id.
    reg.history._append_replay_event(
        provider_id="provider-1", revision=1, event_id=reg.get_provider("provider-1").last_event_id
    )
    assert len(reg.history._lines()) == n1


# --- crash recovery ---------------------------------------------------------


def test_crash_before_registry_replace(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    journal = tmp_path / ".registry_txn.json"
    journal.write_text(
        json.dumps(
            {
                "old_revision": 0,
                "new_revision": 1,
                "event": {
                    "event_id": "ev-x",
                    "provider_id": "provider-1",
                    "revision": 1,
                    "event_type": "provider.upsert",
                    "changed_fields": ["provider"],
                    "reason": "r",
                    "source_metadata": {},
                    "timestamp": "2026-09-01T00:00:00+00:00",
                },
            }
        ),
        encoding="utf-8",
    )
    # registry still at old revision -> discard unapplied journal
    reg.recover_pending_transaction()
    assert not journal.exists()
    assert reg.get_provider("provider-1") is None


def test_crash_after_registry_replace(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    reg.upsert_provider(_provider(), reason="r", source_metadata={})
    got = reg.get_provider("provider-1")
    # remove the history event to simulate a crash between replace and append
    reg.history_path.write_text("", encoding="utf-8")
    journal = tmp_path / ".registry_txn.json"
    journal.write_text(
        json.dumps(
            {
                "old_revision": 0,
                "new_revision": 1,
                "event": {
                    "event_id": got.last_event_id,
                    "provider_id": "provider-1",
                    "revision": 1,
                    "event_type": "provider.upsert",
                    "changed_fields": ["provider"],
                    "reason": "r",
                    "source_metadata": {},
                    "timestamp": "2026-09-01T00:00:00+00:00",
                },
            }
        ),
        encoding="utf-8",
    )
    reg.recover_pending_transaction()
    assert not journal.exists()
    lines = reg.history._lines()
    assert len(lines) == 1
    assert json.loads(lines[0])["event_id"] == got.last_event_id


def test_recovery_unexpected_revision_fails_closed(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    reg.upsert_provider(_provider(), reason="r", source_metadata={})
    journal = tmp_path / ".registry_txn.json"
    journal.write_text(
        json.dumps(
            {
                "old_revision": 5,
                "new_revision": 6,
                "event": {
                    "event_id": "ev-y",
                    "provider_id": "provider-1",
                    "revision": 6,
                    "event_type": "provider.upsert",
                    "changed_fields": ["provider"],
                    "reason": "r",
                    "source_metadata": {},
                    "timestamp": "2026-09-01T00:00:00+00:00",
                },
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(RegistryError):
        reg.recover_pending_transaction()
    # journal must remain for manual inspection
    assert journal.exists()


def test_recovery_corrupt_journal_fails_closed(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    journal = tmp_path / ".registry_txn.json"
    journal.write_text("{not json", encoding="utf-8")
    with pytest.raises(RegistryError):
        reg.recover_pending_transaction()


# --- no deletion API --------------------------------------------------------


def test_no_delete_operation(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    assert not hasattr(reg, "delete_provider")
    assert not hasattr(reg, "remove_provider")
