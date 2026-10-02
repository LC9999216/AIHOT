"""End-to-end stage-one pipeline (TASK-010).

Deterministic given the same seed fixture, config, and explicit ``as_of``:
  import seed -> candidates
  -> resolve and fetch candidate evidence URLs
  -> validate officiality via trust anchors
  -> grounded extraction through an injected LLM boundary
  -> scores
  -> confirm FREE_CONFIRMED providers through confirm_provider()

Running twice with identical inputs produces a byte-identical Registry and no
new no-op History events. The default missing extractor fails closed.
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, Future
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .collectors.registry_seed import RegistrySeedImporter
from .config import DEFAULT_CONFIG_DIR, load_sources
from .discovery.store import CandidateStore
from .evidence.fetcher import SafeFetcher
from .evidence.models import Evidence
from .evidence.resolver import EvidenceResolver
from .evidence.store import EvidenceStore
from .evidence.validator import (
    ContradictionError,
    OfficialEvidenceValidator,
    load_trust_anchors,
)
from .llm.models import ExtractionResult
from .registry.confirmation import ConfirmationError, ConfirmationInput, confirm_provider
from .registry.schema import ProviderStatus
from .registry.store import ProviderRegistry
from .scoring.config import load_scoring_config
from .scoring.scores import free_score, verification_confidence

# Concurrency bounds for the two IO-bound stages. Fetching is bounded per
# candidate and persistence is serialized inside EvidenceStore/ProviderRegistry.
EVIDENCE_BUILD_WORKERS = 8
EXTRACTION_WORKERS = 4


class Pipeline:
    """Rerunnable offline stage-one pipeline."""

    def __init__(
        self,
        data_dir: Path,
        seed_path: Optional[Path] = None,
        as_of: Optional[datetime] = None,
        scoring_path: Optional[Path] = None,
        sources_path: Optional[Path] = None,
        resolver: Optional[EvidenceResolver] = None,
        fetcher: Optional[SafeFetcher] = None,
        extractor: Any = None,
    ):
        self.data_dir = Path(data_dir)
        self.seed_path = Path(seed_path) if seed_path else None
        self.as_of = as_of or datetime(2026, 9, 1, tzinfo=timezone.utc)
        if self.as_of.tzinfo is None:
            self.as_of = self.as_of.replace(tzinfo=timezone.utc)
        self.scoring_path = Path(scoring_path) if scoring_path else DEFAULT_CONFIG_DIR / "scoring.yaml"
        self.sources_path = Path(sources_path) if sources_path else DEFAULT_CONFIG_DIR / "sources.yaml"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.candidate_store = CandidateStore(self.data_dir / "candidates.json")
        self.evidence_store = EvidenceStore(self.data_dir / "evidence.json")
        self.registry = ProviderRegistry(
            providers_path=self.data_dir / "providers.json",
            history_path=self.data_dir / "history.jsonl",
        )
        anchors = load_trust_anchors(self.sources_path)
        sources_config = load_sources(self.sources_path)
        reviewed = sources_config.get("reviewed_evidence") or {}
        self.reviewed_evidence: Dict[str, List[Dict[str, str]]] = {}
        if isinstance(reviewed, dict):
            for provider_id, entries in reviewed.items():
                if not isinstance(entries, list):
                    continue
                normalized: List[Dict[str, str]] = []
                for entry in entries:
                    if isinstance(entry, str):
                        normalized.append({"url": entry, "source_type": ""})
                    elif isinstance(entry, dict) and isinstance(entry.get("url"), str):
                        source_type = str(entry.get("source_type") or "")
                        if source_type not in {"", "pricing", "api-docs", "docs", "blog", "github"}:
                            continue
                        normalized.append({"url": entry["url"], "source_type": source_type})
                if normalized:
                    self.reviewed_evidence[str(provider_id)] = normalized
        self.validator = OfficialEvidenceValidator(anchors=anchors)
        self.scoring_config = load_scoring_config(self.scoring_path)
        self.resolver = resolver or EvidenceResolver()
        self.fetcher = fetcher or SafeFetcher()
        self.extractor = extractor
        self._resolved_evidence: Dict[str, Evidence] = {}
        self._unresolved_contradictions: Dict[str, List[str]] = {}
        self._resolution_ready = False

    # --- step 1: import seed -------------------------------------------------

    def import_seed(self) -> Dict[str, int]:
        if self.seed_path is None:
            raise ValueError("seed_path is required for import_seed")
        result = RegistrySeedImporter().load(self.seed_path)
        counts = self.candidate_store.ingest(result.entries)
        return {
            "version": result.version,
            "candidates_created": counts.candidates_created,
            "candidates_merged": counts.candidates_merged,
            "observations_added": counts.observations_added,
            "unchanged": counts.unchanged,
            "rejected": counts.rejected,
            "errors": counts.errors,
        }

    # --- step 2: build evidence ---------------------------------------------

    def build_evidence(self) -> int:
        """Resolve, fetch, and persist evidence from HTTP responses only.

        Candidates are fetched concurrently (IO bound). Within one candidate
        the proposed URLs are still processed strictly in order, so per-URL
        ordering for one candidate is preserved.
        """
        with ThreadPoolExecutor(max_workers=EVIDENCE_BUILD_WORKERS) as pool:
            counts = list(
                pool.map(
                    self._build_evidence_for_candidate,
                    self.candidate_store.list_candidates(),
                )
            )
        self._resolution_ready = False
        return sum(counts)

    def _build_evidence_for_candidate(self, candidate) -> int:
        created = 0
        observations = [obs.model_dump(mode="json") for obs in candidate.observations]
        reviewed_entries = self.reviewed_evidence.get(candidate.candidate_id, [])
        reviewed_types = {
            entry["url"]: entry["source_type"] for entry in reviewed_entries
        }
        urls = [entry["url"] for entry in reviewed_entries]
        if not reviewed_entries:
            urls = self.resolver.propose_urls(
                candidate_domain=candidate.canonical_domain_hint,
                observations=observations,
            )
        for url in urls:
            try:
                result = self.fetcher.fetch(url, provider_id=candidate.candidate_id)
            except Exception:  # noqa: BLE001 - one URL must not stop a candidate
                continue
            if not 200 <= result.status < 300:
                continue
            # The excerpt and claim are DERIVED from the fetched body inside
            # from_fetch so the stored evidence stays bound to the content
            # the SafeFetcher actually retrieved (no caller-supplied text).
            try:
                evidence = Evidence.from_fetch(
                    result,
                    provider_id=candidate.candidate_id,
                    source_type=(reviewed_types.get(url) or _guess_source_type(result.final_url or url)),
                    excerpt_length=4000,
                    retrieved_at=self.as_of,
                    candidate_id=candidate.candidate_id,
                    title=result.final_url or url,
                )
            except ValueError:
                continue
            if self.evidence_store.upsert(evidence):
                created += 1
        return created

    # --- step 3: validate officiality ----------------------------------------

    def validate_evidence(self) -> Dict[str, int]:
        counts = {"OFFICIAL": 0, "LIKELY_OFFICIAL": 0, "THIRD_PARTY": 0, "REJECTED": 0, "UNCONFIRMED": 0}
        for evidence in self.evidence_store.list():
            validated = self.validator.validate(evidence)
            counts[validated.officiality.value] = counts.get(validated.officiality.value, 0) + 1
            self.evidence_store.upsert(validated)
        self._resolution_ready = False
        return counts

    def resolve_contradictions(self) -> Dict[str, int]:
        """Resolve validated evidence before extraction or scoring."""
        resolved: Dict[str, Evidence] = {}
        unresolved: Dict[str, List[str]] = {}
        for candidate in self.candidate_store.list_candidates():
            items = [
                evidence
                for evidence in self.evidence_store.list()
                if evidence.candidate_id == candidate.candidate_id
            ]
            if not items:
                continue
            try:
                decision = self.validator.resolve_contradictions(items)
            except ContradictionError as exc:
                unresolved[candidate.candidate_id] = [str(exc)]
                continue
            if decision["unresolved"]:
                unresolved[candidate.candidate_id] = list(decision["unresolved"])
                continue
            resolved[candidate.candidate_id] = decision["winner"]
        self._resolved_evidence = resolved
        self._unresolved_contradictions = unresolved
        self._resolution_ready = True
        return {"resolved": len(resolved), "unresolved": len(unresolved)}

    # --- step 4: extraction --------------------------------------------------

    def _extraction_for(self, candidate_id: str) -> Optional[ExtractionResult]:
        evidence = self._resolved_evidence.get(candidate_id)
        if evidence is None or self.extractor is None:
            return None
        return self.extractor.extract(evidence, as_of=self.as_of.isoformat())

    # --- step 5+6: score and confirm -----------------------------------------

    def score_and_confirm(self) -> Dict[str, int]:
        """Score each candidate and confirm when every hard gate passes.

        LLM extraction is IO bound, so the candidates that actually need
        extraction are processed concurrently (max 4 in flight). Scoring and
        confirmation stay sequential: mutation of the registry and the
        candidate outcome sequence must remain deterministic.

        Candidates whose provider record already carries the identical evidence
        id snapshot AND a confirmed extraction from the previous commit are
        reused as-is ("skip_uptodate"): neither extraction nor confirmation is
        rerun. Candidates whose last extraction failed or that were never
        confirmed have no snapshot and are always retried.
        """
        if not self._resolution_ready:
            self.resolve_contradictions()
        outcomes = {
            "free_confirmed": 0,
            "providers_created": 0,
            "providers_updated": 0,
            "unchanged": 0,
            "errors": 0,
            "skip_uptodate": 0,
            "candidate_outcomes": [],
        }

        plans = self._plan_score_and_confirm(outcomes)

        # Extraction runs concurrently; failures are reported per candidate in
        # deterministic candidate order during the second pass.
        for plan in plans:
            future: Optional[Future] = plan["extraction_future"]
            if future is None:
                continue
            try:
                plan["extraction"] = future.result()
            except Exception as exc:  # noqa: BLE001 - re-raised in sequence
                plan["extraction_error"] = exc
            finally:
                plan["extraction_future"] = None

        for plan in plans:
            try:
                outcome = plan["outcome"]
                if outcome == "skip_uptodate":
                    outcomes["unchanged"] += 1
                    continue
                if outcome == "unchanged":
                    # no_evidence / unresolved contradiction / no resolved
                    # winner: already counted in the planning pass
                    report = plan.get("outcome_report")
                    if report is not None:
                        outcomes["candidate_outcomes"].append(report)
                    continue
                candidate = plan["candidate"]
                evidence_items = plan["evidence_items"]
                if plan.get("extraction_error") is not None:
                    raise plan["extraction_error"]
                extraction = plan["extraction"]
                if extraction is None or not extraction.ok:
                    # ungrounded extraction: recorded, retryable, not confirmed
                    outcomes["candidate_outcomes"].append(
                        {
                            "candidate_id": candidate.candidate_id,
                            "outcome": (
                                "extractor_unavailable"
                                if extraction is None
                                else "extraction_failed"
                            ),
                            "reason": (
                                "real grounded extractor is not configured"
                                if extraction is None
                                else extraction.failure_reason or "unspecified"
                            ),
                        }
                    )
                    outcomes["unchanged"] += 1
                    continue
                provider_id = plan["provider_id"]
                before = plan["before"]
                winner = plan["winner"]
                vc = verification_confidence(evidence_items, self.scoring_config, self.as_of)
                requirements = _requirements_for(extraction)
                api = _api_for(extraction)
                fs = free_score(
                    _offer_from_extraction(extraction),
                    requirements,
                    api,
                    models=_models_for(extraction),
                    has_documented_limits=False,
                    _config=self.scoring_config,
                    as_of=self.as_of,
                )
                confirm_result = confirm_provider(
                    ConfirmationInput(
                        provider_name=candidate.provider_name or candidate.candidate_id,
                        canonical_domain=candidate.canonical_domain_hint or "",
                        evidence=winner,
                        extraction=extraction,
                        verification_confidence=vc,
                        free_score=fs,
                        as_of=self.as_of,
                        source_metadata={
                            "pipeline": "run-stage-one",
                            "as_of": self.as_of.isoformat(),
                        },
                        reason="stage-one pipeline confirmation from anchored official evidence",
                        models=_models_for(extraction),
                        has_documented_limits=False,
                        official_docs=[
                            evidence.url
                            for evidence in evidence_items
                            if evidence.officiality.value == "OFFICIAL"
                        ],
                        evidence_ids_snapshot=plan["evidence_ids"],
                    ),
                    self.registry,
                    self.validator,
                )
                plan["confirmed_revision"] = confirm_result.revision
            except ConfirmationError as exc:
                # a hard gate rejected this candidate; nothing was mutated
                outcomes["candidate_outcomes"].append(
                    {
                        "candidate_id": plan["candidate"].candidate_id,
                        "outcome": "confirmation_rejected",
                        "reason": str(exc),
                    }
                )
                outcomes["unchanged"] += 1
                continue
            except Exception as exc:  # noqa: BLE001 - isolate and report safely
                outcomes["candidate_outcomes"].append(
                    {
                        "candidate_id": plan["candidate"].candidate_id,
                        "outcome": "processing_error",
                        "reason": _sanitized_exception(exc),
                    }
                )
                outcomes["errors"] += 1
                continue

            outcomes["free_confirmed"] += 1
            before = plan["before"]
            if before is None:
                outcomes["providers_created"] += 1
            elif plan["confirmed_revision"] > before.revision:
                outcomes["providers_updated"] += 1
            else:
                outcomes["unchanged"] += 1

        skip_uptodate = outcomes["skip_uptodate"]
        # Skip statistics are printed before the run summary counts.
        print(f"skip_uptodate={skip_uptodate}")
        return outcomes

    def _plan_score_and_confirm(self, outcomes: Dict[str, int]) -> List[Dict[str, Any]]:
        """First pass: classify candidates and submit concurrent extractions."""
        plans: List[Dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=EXTRACTION_WORKERS) as pool:
            for candidate in self.candidate_store.list_candidates():
                plan: Dict[str, Any] = {
                    "candidate": candidate,
                    "evidence_items": [],
                    "evidence_ids": [],
                    "winner": None,
                    "provider_id": None,
                    "before": None,
                    "extraction": None,
                    "extraction_error": None,
                    "extraction_future": None,
                    "confirmed_revision": 0,
                    "outcome": "confirm",
                }
                evidence_items = [
                    e
                    for e in self.evidence_store.list()
                    if e.candidate_id == candidate.candidate_id
                ]
                plan["evidence_items"] = evidence_items
                plan["evidence_ids"] = sorted(
                    e.evidence_id for e in evidence_items if e.evidence_id
                )
                if not evidence_items:
                    outcomes["unchanged"] += 1
                    plan["outcome"] = "no_evidence"
                    plans.append(plan)
                    continue
                winner = self._resolved_evidence.get(candidate.candidate_id)
                plan["winner"] = winner
                unresolved = self._unresolved_contradictions.get(candidate.candidate_id)
                if winner is None or unresolved:
                    reasons = unresolved or ["no evidence winner"]
                    plan["outcome"] = "unchanged"
                    plan["outcome_report"] = {
                        "candidate_id": candidate.candidate_id,
                        "outcome": "unresolved_contradiction" if unresolved else "no_resolved_evidence",
                        "reason": "; ".join(reasons),
                    }
                    outcomes["unchanged"] += 1
                    plans.append(plan)
                    continue
                provider_id = _slug(candidate.provider_name or candidate.candidate_id)
                plan["provider_id"] = provider_id
                before = self.registry.get_provider(provider_id) or self.registry.find_by_domain(
                    candidate.canonical_domain_hint
                )
                plan["before"] = before
                if self._provider_is_uptodate(before, plan["evidence_ids"]):
                    # Same resolved evidence id set and a confirmed provider
                    # record from the previous commit: reuse that state.
                    plan["outcome"] = "skip_uptodate"
                    outcomes["skip_uptodate"] += 1
                    outcomes["unchanged"] += 1
                    plans.append(plan)
                    continue
                if self.extractor is not None:
                    plan["extraction_future"] = pool.submit(
                        self._extraction_for, candidate.candidate_id
                    )
                plans.append(plan)
        return plans

    def _provider_is_uptodate(self, before: Any, evidence_ids: List[str]) -> bool:
        """True when the provider record already reflects these evidence ids.

        The snapshot is written under provider metadata at confirmation time.
        A provider created by another path (no snapshot) is always reprocessed.
        """
        if before is None or not before.evidence_ids:
            return False
        if before.status is not ProviderStatus.FREE_CONFIRMED:
            return False
        snapshot = (before.metadata or {}).get("pipeline_evidence_ids")
        if not isinstance(snapshot, list):
            return False
        return sorted(snapshot) == sorted(evidence_ids)

    # --- full run ------------------------------------------------------------

    def run(self) -> Dict[str, Any]:
        """Run the whole stage-one flow and return the spec summary."""
        seed_summary = self.import_seed() if self.seed_path else {"skipped": True}
        candidates = self.candidate_store.list_candidates()
        evidence_built = self.build_evidence()
        validation = self.validate_evidence()
        contradictions = self.resolve_contradictions()
        outcomes = self.score_and_confirm()

        status_counts = {
            "free_confirmed": 0,
            "uncertain": 0,
            "not_free": 0,
            "expired": 0,
            "rejected": 0,
        }
        for provider in self.registry.list_providers():
            status_counts[_status_key(provider.status)] = (
                status_counts.get(_status_key(provider.status), 0) + 1
            )

        return {
            "candidates_processed": len(candidates),
            "providers_created": outcomes["providers_created"],
            "providers_updated": outcomes["providers_updated"],
            "free_confirmed": status_counts["free_confirmed"],
            "uncertain": status_counts["uncertain"],
            "not_free": status_counts["not_free"],
            "expired": status_counts["expired"],
            "rejected": status_counts["rejected"],
            "unchanged": outcomes["unchanged"],
            "errors": outcomes["errors"],
            "skip_uptodate": outcomes.get("skip_uptodate", 0),
            "detail": {
                "import_seed": seed_summary,
                "evidence_built": evidence_built,
                "validation": validation,
                "contradictions": contradictions,
                "providers": len(self.registry.list_providers()),
                "candidate_outcomes": outcomes["candidate_outcomes"],
            },
        }


def _status_key(status) -> str:
    value = status.value if hasattr(status, "value") else str(status)
    return {
        "FREE_CONFIRMED": "free_confirmed",
        "UNCERTAIN": "uncertain",
        "NOT_FREE": "not_free",
        "EXPIRED": "expired",
        "REJECTED": "rejected",
    }.get(value, "uncertain")


def _sanitized_exception(exc: Exception) -> str:
    """Return bounded diagnostic text without common credential forms."""
    message = " ".join(str(exc).split())
    message = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "[REDACTED]", message)
    message = re.sub(r"(?i)\bbearer\s+\S+", "Bearer [REDACTED]", message)
    return f"{type(exc).__name__}: {message}"[:500]


def _slug(name: str) -> str:
    import re

    slug = re.sub(r"[^a-z0-9]+", "-", str(name).strip().lower()).strip("-")
    return slug or "provider"


def _guess_source_type(url: str) -> str:
    if "pricing" in url or "plan" in url:
        return "pricing"
    if "api" in url or "developer" in url or "reference" in url:
        return "api-docs"
    if "docs" in url or "documentation" in url:
        return "docs"
    if "blog" in url or "news" in url:
        return "blog"
    return "page"


def _requirements_for(extraction: ExtractionResult):
    from .registry.schema import ProviderRequirements

    return ProviderRequirements(
        signup_required=extraction.signup_required,
        phone_required=extraction.phone_required,
        card_required=extraction.card_required,
        commercial_use_allowed=extraction.commercial_use_allowed,
        regional_restrictions=None,
    )


def _api_for(extraction: ExtractionResult):
    from .registry.schema import ProviderApi

    return ProviderApi(
        base_url=extraction.base_url,
        openai_compatible=extraction.openai_compatible,
    )


def _models_for(extraction: ExtractionResult) -> List[str]:
    models = [str(model) for model in extraction.models]
    seen = set()
    return [m for m in models if not (m in seen or seen.add(m))]


def _offer_from_extraction(extraction: ExtractionResult):
    from .registry.schema import (
        AccessMethod,
        FreeOffer,
        OfferKind,
        OfferStatus,
        QuotaMode,
        RenewalPeriod,
    )

    try:
        kind = OfferKind(extraction.offer_kind) if extraction.offer_kind else OfferKind.unknown
    except ValueError:
        kind = OfferKind.unknown
    try:
        quota = QuotaMode(extraction.quota_mode) if extraction.quota_mode else QuotaMode.unknown
    except ValueError:
        quota = QuotaMode.unknown
    try:
        renewal = RenewalPeriod(extraction.renewal_period) if extraction.renewal_period else None
    except ValueError:
        renewal = None
    try:
        access = AccessMethod(extraction.access_method) if extraction.access_method else AccessMethod.unknown
    except ValueError:
        access = AccessMethod.unknown
    return FreeOffer(
        offer_kind=kind,
        quota_mode=quota,
        renewal_period=renewal,
        access_method=access,
        offer_status=OfferStatus.unknown,
        description=extraction.description,
        quota_text=extraction.quota_text,
        expires_at=extraction.expires_at,
    )


def _plain_text_excerpt(text: str) -> str:
    import re

    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()
