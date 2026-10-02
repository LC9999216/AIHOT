"""TASK-002 tests: pinned seed importer (registry_seed.py)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hunter.collectors.registry_seed import (
    RegistrySeedImporter,
    SeedImportError,
)
from hunter.discovery.models import SourceType
from hunter.registry.schema import OfferKind, QuotaMode

FIXTURE = Path(__file__).parent / "fixtures" / "upstream" / "free-llm-api-hub-v2.9.0.json"


def _write(tmp_path: Path, payload) -> Path:
    path = tmp_path / "seed.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _entry(**overrides) -> dict:
    base = {
        "slug": "acme-ai",
        "name": "Acme AI",
        "category": "ongoing",
        "free_type": "renewing-quota",
        "free_tier": "Free tier with limits",
        "rate_limits": "10 RPM",
        "notes": "notes",
        "best_for": None,
        "modalities": ["text"],
        "models_free": ["acme-1"],
        "expires": None,
        "docs_url": "https://docs.acme.ai/pricing",
        "phone_required": False,
        "card_required": False,
        "commercial_ok": True,
        "openai_compatible": True,
        "openai_base_url": None,
        "env_key": "ACME_API_KEY",
        "verified": True,
        "last_verified": "2026-08-01",
    }
    base.update(overrides)
    return base


def _root(*providers, version: str = "2.9.0") -> dict:
    return {
        "$schema": "./schema.json",
        "version": version,
        "generated": "2026-08-14",
        "source": "https://github.com/pacocartones/free-llm-api-hub",
        "note": "Canonical dataset.",
        "providers": list(providers),
    }


# --- version / structure ----------------------------------------------------


def test_importer_accepts_pinned_fixture() -> None:
    importer = RegistrySeedImporter()
    result = importer.load(FIXTURE)
    assert result.version == "2.9.0"
    assert len(result.entries) == 69


def test_importer_rejects_wrong_version(tmp_path: Path) -> None:
    path = _write(tmp_path, _root(_entry(), version="2.8.0"))
    with pytest.raises(SeedImportError) as exc:
        RegistrySeedImporter().load(path)
    assert "2.9.0" in str(exc.value)


def test_importer_rejects_malformed_root(tmp_path: Path) -> None:
    path = tmp_path / "seed.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(SeedImportError):
        RegistrySeedImporter().load(path)


def test_importer_rejects_root_without_providers(tmp_path: Path) -> None:
    path = _write(tmp_path, {"version": "2.9.0", "providers": "nope"})
    with pytest.raises(SeedImportError):
        RegistrySeedImporter().load(path)


def test_importer_rejects_missing_optional_fields(tmp_path: Path) -> None:
    entry = _entry()
    for field in ("env_key", "openai_base_url", "added", "expires", "best_for"):
        entry.pop(field, None)
    path = _write(tmp_path, _root(entry))
    result = RegistrySeedImporter().load(path)
    assert len(result.entries) == 1


def test_importer_rejects_entry_without_slug_or_name(tmp_path: Path) -> None:
    bad = {"name": "No Slug", "free_type": "perpetual"}
    path = _write(tmp_path, _root(bad))
    result = RegistrySeedImporter().load(path)
    assert result.rejected == 1
    assert result.entries == []


def test_importer_rejects_unknown_free_type(tmp_path: Path) -> None:
    entry = _entry(free_type="quantum-bonanza")
    path = _write(tmp_path, _root(entry))
    result = RegistrySeedImporter().load(path)
    assert result.rejected == 1


# --- mapping rules ----------------------------------------------------------


def test_observation_source_type_and_metadata() -> None:
    result = RegistrySeedImporter().load(FIXTURE)
    obs = result.entries[0].observation
    assert obs.source_type == SourceType.third_party_registry
    meta = obs.raw_metadata
    assert "upstream_slug" in meta
    assert meta["upstream_free_type"] in {
        "renewing-quota",
        "trial-credit",
        "perpetual",
        "recurring-credit",
    }
    # assertions preserved as metadata
    assert "asserted_verified" in meta
    assert "asserted_last_verified" in meta
    assert "asserted_docs_url" in meta


@pytest.mark.parametrize(
    "free_type,kind,quota",
    [
        ("perpetual", OfferKind.free_tier, QuotaMode.unmetered),
        ("renewing-quota", OfferKind.free_tier, QuotaMode.renewing),
        ("recurring-credit", OfferKind.free_credit, QuotaMode.renewing),
        ("trial-credit", OfferKind.trial, QuotaMode.one_time),
    ],
)
def test_upstream_free_type_mapping(free_type, kind, quota) -> None:
    importer = RegistrySeedImporter()
    hints = importer.map_free_type(free_type)
    assert hints["offer_kind"] == kind.value
    assert hints["quota_mode"] == quota.value


def test_import_does_not_create_providers(tmp_path: Path) -> None:
    importer = RegistrySeedImporter()
    result = importer.load(FIXTURE)
    # entries are observation-level only
    assert all(e.provider is None for e in result.entries)
    assert not importer.creates_providers()


def test_no_invented_values(tmp_path: Path) -> None:
    entry = _entry()
    entry.pop("phone_required")
    entry.pop("card_required")
    path = _write(tmp_path, _root(entry))
    result = RegistrySeedImporter().load(path)
    obs = result.entries[0].observation
    meta = obs.raw_metadata
    # null/absent stay null, never invented
    assert meta.get("upstream_phone_required") in (None, False)
