"""TASK-002: pinned seed importer for ``pacocartones/free-llm-api-hub`` v2.9.0.

Reads a local JSON file only; never downloads data. Requires root
``version == "2.9.0"`` and a compatible Provider collection. Converts upstream
free-type values into orthogonal offer-field hints labeled as upstream
assertions, and never transfers upstream trust into this project.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..discovery.models import CandidateObservation, SourceType
from ..discovery.store import ObservationWithCandidate
from ..registry.schema import (
    AccessMethod,
    FreeOffer,
    OfferKind,
    OfferStatus,
    QuotaMode,
    map_legacy_free_type,
)

PINNED_VERSION = "2.9.0"

# Upstream free_type spellings -> canonical offer-field hints.
# `perpetual` mirrors legacy `permanent_free`; `recurring-credit` is a
# recurring free credit; `renewing-quota` and `trial-credit` map directly.
_UPSTREAM_FREE_TYPE_HINTS: Dict[str, Dict[str, Any]] = {
    "perpetual": {"offer_kind": "free_tier", "quota_mode": "unmetered"},
    "recurring-credit": {"offer_kind": "free_credit", "quota_mode": "renewing"},
    "renewing-quota": {"offer_kind": "free_tier", "quota_mode": "renewing"},
    "trial-credit": {"offer_kind": "trial", "quota_mode": "one_time"},
}

# Keys we always preserve as bounded metadata (no secrets, no full dumps).
_METADATA_KEYS = (
    "slug",
    "name",
    "category",
    "free_type",
    "free_tier",
    "rate_limits",
    "notes",
    "best_for",
    "modalities",
    "models_free",
    "expires",
    "docs_url",
    "phone_required",
    "card_required",
    "commercial_ok",
    "openai_compatible",
    "openai_base_url",
    "env_key",
    "added",
)

# Known legacy free-type names accepted through map_legacy_free_type.
_LEGACY_FREE_TYPES = {
    "permanent_free",
    "renewing_quota",
    "daily_free",
    "monthly_free",
    "signup_credit",
    "trial_credit",
    "promotion",
    "keyless_free",
    "expired",
}


class SeedImportError(Exception):
    """Raised when the upstream seed file is not a compatible pinned dataset."""


@dataclass
class SeedImportResult:
    version: str
    entries: List[ObservationWithCandidate]
    rejected: int = 0


class RegistrySeedImporter:
    """Import the pinned v2.9.0 dataset into CandidateObservations only."""

    def load(self, path: Path) -> SeedImportResult:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            raise SeedImportError(f"cannot read seed file {path}: {exc}") from exc

        if not isinstance(payload, dict):
            raise SeedImportError("seed root must be a JSON object")

        version = payload.get("version")
        if version != PINNED_VERSION:
            raise SeedImportError(
                f"incompatible seed version {version!r}; pinned version is {PINNED_VERSION}"
            )

        providers = payload.get("providers")
        if not isinstance(providers, list):
            raise SeedImportError("seed root must contain a providers list")

        entries: List[ObservationWithCandidate] = []
        rejected = 0
        for provider in providers:
            if not isinstance(provider, dict):
                rejected += 1
                continue
            try:
                entries.append(self._entry_for(provider))
            except ValueError:
                rejected += 1
        return SeedImportResult(version=version, entries=entries, rejected=rejected)

    def creates_providers(self) -> bool:
        return False

    @staticmethod
    def _free_type_supported(free_type: Optional[str]) -> bool:
        if free_type is None:
            return True
        key = str(free_type).strip().lower()
        return key in _UPSTREAM_FREE_TYPE_HINTS or key in _LEGACY_FREE_TYPES

    def map_free_type(self, free_type: Optional[str]) -> Dict[str, str]:
        """Map an upstream free-type value to orthogonal offer-field hints."""
        if free_type is None:
            return {"offer_kind": "unknown", "quota_mode": "unknown"}
        key = str(free_type).strip().lower()
        if key in _UPSTREAM_FREE_TYPE_HINTS:
            hints = _UPSTREAM_FREE_TYPE_HINTS[key]
            return {"offer_kind": hints["offer_kind"], "quota_mode": hints["quota_mode"]}
        # Fall back to AGENTS.md legacy table names (e.g. permanent_free).
        mapped = map_legacy_free_type(key)
        return {
            "offer_kind": mapped.offer_kind.value,
            "quota_mode": mapped.quota_mode.value,
            "renewal_period": mapped.renewal_period.value if mapped.renewal_period else None,
        }

    def _entry_for(self, provider: Dict[str, Any]) -> ObservationWithCandidate:
        slug = provider.get("slug")
        name = provider.get("name")
        if not slug or not name:
            raise ValueError("provider entry requires slug and name")

        free_type = provider.get("free_type")
        hints = self.map_free_type(free_type)
        if not self._free_type_supported(free_type):
            raise ValueError(f"unknown free_type {free_type!r}")

        docs_url = provider.get("docs_url")
        domain_hint = self._domain_from_url(docs_url) if docs_url else None

        metadata: Dict[str, Any] = {
            "upstream_slug": slug,
            "upstream_name": name,
            "upstream_category": provider.get("category"),
            "upstream_free_type": free_type,
            "offer_hints": hints,
        }
        for key in _METADATA_KEYS:
            if key in provider:
                metadata[f"upstream_{key}"] = provider[key]
        # Third-party assertions, preserved as metadata only.
        metadata["asserted_verified"] = provider.get("verified")
        metadata["asserted_last_verified"] = provider.get("last_verified")
        metadata["asserted_docs_url"] = docs_url

        claim = provider.get("free_tier") or provider.get("notes") or f"{name} free offer"

        observation = CandidateObservation(
            observation_id=f"seed-{slug}",
            source_type=SourceType.third_party_registry,
            source_url="https://github.com/pacocartones/free-llm-api-hub/blob/v2.9.0/data/providers.json",
            source_title=f"free-llm-api-hub v{PINNED_VERSION}",
            claim=claim,
            matched_query=None,
            discovered_at=self._discovered_at(),
            raw_metadata=metadata,
        )
        return ObservationWithCandidate(
            observation=observation,
            candidate_id=slug,
            provider_name=name,
            canonical_domain_hint=domain_hint,
        )

    @staticmethod
    def _discovered_at():
        # Pinned import time is deterministic per run from the dataset timestamp.
        from datetime import datetime, timezone

        return datetime.now(timezone.utc)

    @staticmethod
    def _domain_from_url(url: str) -> Optional[str]:
        try:
            from urllib.parse import urlparse

            host = urlparse(url).hostname
            return host.lower().rstrip(".") if host else None
        except ValueError:
            return None
