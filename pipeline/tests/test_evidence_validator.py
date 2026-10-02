"""TASK-008 tests: trust anchors, officiality, contradiction policy.

Spec coverage: exact domain, permitted/prohibited subdomains, trailing-dot and
IDNA normalization, lookalike domains, URL shorteners, imported URL without an
anchor, exact GitHub mapping, fork/username similarity, each precedence level,
newer same-priority evidence, missing dates, stale promotion vs current
pricing, unresolved conflicts, and proof that LIKELY_OFFICIAL never counts as
official.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from datetime import datetime, timezone

from hunter.evidence.models import Evidence, EvidenceProvenance, Officiality
from hunter.evidence.validator import (
    RULE_ALLOWED_SUBDOMAIN,
    RULE_EXACT_DOMAIN,
    RULE_GITHUB_MAPPING,
    RULE_NO_ANCHOR_FOR_PROVIDER,
    RULE_NO_MATCH_THIRD_PARTY,
    RULE_SAME_REGISTRABLE_DOMAIN,
    ContradictionError,
    OfficialEvidenceValidator,
    TrustAnchor,
    github_repo_from_url,
    load_trust_anchors,
    match_anchor,
)


def _anchor(**kw) -> TrustAnchor:
    base = dict(
        provider_id="acme",
        domains=["acme.ai"],
        allow_subdomains=False,
        provenance="manual",
        reviewed_at="2026-09-01",
    )
    base.update(kw)
    return TrustAnchor(**base)


def _validator(anchors=None) -> OfficialEvidenceValidator:
    return OfficialEvidenceValidator(
        anchors=anchors
        if anchors is not None
        else [
            _anchor(),
            _anchor(
                provider_id="beta",
                domains=["beta.dev"],
                allow_subdomains=True,
                github_organizations=["beta-org"],
                github_repositories=["beta/llm"],
            ),
        ]
    )


def _evidence(url: str, source_type: str = "pricing", provider_id: str = "acme", **kw) -> Evidence:
    # For OFFICIAL tests, auto-attach provenance
    officiality = kw.pop("officiality", Officiality.UNCONFIRMED)
    provenance = kw.pop("provenance", None)
    if provenance is None and officiality is Officiality.OFFICIAL:
        provenance = EvidenceProvenance(
            retrieval_method="safe_fetch",
            original_url=url,
            final_url=url,
            http_status=200,
            content_sha256="a" * 64,
            retrieved_from_origin=True,
            retrieved_at=datetime.fromisoformat(str(kw.get("retrieved_at", "2026-08-01T00:00:00+00:00"))),
        )
    return Evidence(
        evidence_id=kw.pop("evidence_id", "ev-1"),
        provider_id=provider_id,
        url=url,
        source_type=source_type,
        officiality=officiality,
        provenance=provenance,
        claim=kw.pop("claim", "free plan"),
        content_excerpt=kw.pop("content_excerpt", "free plan"),
        retrieved_at=kw.pop("retrieved_at", "2026-08-01T00:00:00+00:00"),
        effective_at=kw.pop("effective_at", None),
        published_at=kw.pop("published_at", None),
        **kw,
    )


# --- anchor loading ---------------------------------------------------------


def test_load_trust_anchors_spec_schema(tmp_path: Path) -> None:
    cfg = tmp_path / "sources.yaml"
    cfg.write_text(
        """
trust_anchors:
  - provider_id: acme
    domains: [acme.ai, acme.com]
    allow_subdomains: true
    github_organizations: [acme-org]
    github_repositories: [acme/llm]
    provenance: "manual review 2026-09-01"
    reviewed_at: "2026-09-01"
""",
        encoding="utf-8",
    )
    anchors = load_trust_anchors(cfg)
    assert len(anchors) == 1
    a = anchors[0]
    assert a.provider_id == "acme"
    assert a.domains == ["acme.ai", "acme.com"]
    assert a.allow_subdomains is True
    assert a.github_organizations == ["acme-org"]
    assert a.github_repositories == ["acme/llm"]
    assert a.reviewed_at == "2026-09-01"


def test_load_trust_anchors_empty(tmp_path: Path) -> None:
    cfg = tmp_path / "sources.yaml"
    cfg.write_text("trust_anchors: []\n", encoding="utf-8")
    assert load_trust_anchors(cfg) == []


def test_anchor_missing_required_fields_rejected(tmp_path: Path) -> None:
    cfg = tmp_path / "sources.yaml"
    cfg.write_text("trust_anchors:\n  - domains: [acme.ai]\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_trust_anchors(cfg)


# --- host normalization and matching ----------------------------------------


def test_exact_domain_match_and_normalization() -> None:
    a = _anchor()
    assert match_anchor(a, "acme.ai") is True
    assert match_anchor(a, "ACME.AI") is True
    assert match_anchor(a, "acme.ai.") is True  # trailing dot removed
    assert match_anchor(a, "  ACME.AI.  ") is True
    assert match_anchor(a, "acme2.ai") is False
    assert match_anchor(a, "notacme.ai") is False
    assert match_anchor(a, "acme.ai.evil.com") is False


def test_subdomain_permitted_only_when_allowed() -> None:
    strict = _anchor(allow_subdomains=False)
    permissive = _anchor(allow_subdomains=True)
    assert match_anchor(strict, "docs.acme.ai") is False
    assert match_anchor(permissive, "docs.acme.ai") is True
    assert match_anchor(permissive, "api.v1.acme.ai") is True
    assert match_anchor(permissive, "acme.ai") is True
    # lookalike never matches
    assert match_anchor(permissive, "acme-ai.com") is False
    assert match_anchor(permissive, "evilacme.ai") is False
    assert match_anchor(permissive, "acme.ai.attacker.net") is False


def test_homograph_and_idna_normalization() -> None:
    a = _anchor()
    # Cyrillic homoglyph must not equal the ASCII anchor
    assert match_anchor(a, "асме.ai") is False
    # IDN anchor normalizes to ASCII and matches its ASCII form
    idn = _anchor(domains=["bücher.example"])
    assert match_anchor(idn, "xn--bcher-kva.example") is True


def test_github_mapping_requires_exact_org_or_repo() -> None:
    a = _anchor(github_organizations=["acme-org"], github_repositories=["acme/llm"])
    assert match_anchor(a, "github.com", github_repo="acme-org/anything") is True
    assert match_anchor(a, "github.com", github_repo="acme/llm") is True
    # fork / username similarity must fail
    assert match_anchor(a, "github.com", github_repo="acme-org-fork/anything") is False
    assert match_anchor(a, "github.com", github_repo="acme-org2/anything") is False
    assert match_anchor(a, "github.com", github_repo="evil/llm") is False
    assert match_anchor(a, "github.com", github_repo="acme/llm-fork") is False


def test_github_repo_from_url() -> None:
    assert github_repo_from_url("https://github.com/acme/llm") == "acme/llm"
    assert github_repo_from_url("https://github.com/acme/llm/tree/main") == "acme/llm"
    assert github_repo_from_url("https://github.com/acme") is None
    assert github_repo_from_url("https://gitlab.com/acme/llm") is None
    assert github_repo_from_url("https://github.com.evil.com/acme/llm") is None


# --- officiality decisions with rule IDs ------------------------------------


def test_anchored_exact_domain_is_official() -> None:
    v = _validator()
    decision = v.evaluate(_evidence("https://acme.ai/pricing", officiality=Officiality.OFFICIAL))
    assert decision.officiality is Officiality.OFFICIAL
    assert decision.rule_id == RULE_EXACT_DOMAIN


def test_anchored_subdomain_is_official_when_allowed() -> None:
    v = _validator()
    decision = v.evaluate(_evidence("https://docs.beta.dev/pricing", provider_id="beta", officiality=Officiality.OFFICIAL))
    assert decision.officiality is Officiality.OFFICIAL
    assert decision.rule_id == RULE_ALLOWED_SUBDOMAIN


def test_prohibited_subdomain_is_likely_official() -> None:
    v = _validator()
    decision = v.evaluate(_evidence("https://docs.acme.ai/pricing"))
    assert decision.officiality is Officiality.LIKELY_OFFICIAL
    assert decision.rule_id == RULE_SAME_REGISTRABLE_DOMAIN


def test_mapped_github_is_official() -> None:
    v = _validator()
    decision = v.evaluate(
        _evidence("https://github.com/beta/llm", source_type="github", provider_id="beta", officiality=Officiality.OFFICIAL)
    )
    assert decision.officiality is Officiality.OFFICIAL
    assert decision.rule_id == RULE_GITHUB_MAPPING


def test_unmapped_github_is_third_party() -> None:
    v = _validator()
    decision = v.evaluate(
        _evidence("https://github.com/random/llm", source_type="github", provider_id="beta")
    )
    assert decision.officiality is Officiality.THIRD_PARTY


def test_imported_url_without_anchor_is_not_official() -> None:
    v = _validator()
    decision = v.evaluate(_evidence("https://acme.ai/pricing", provider_id="unknown-provider"))
    assert decision.officiality is Officiality.LIKELY_OFFICIAL
    assert decision.rule_id == RULE_NO_ANCHOR_FOR_PROVIDER


def test_no_anchors_at_all_never_official() -> None:
    v = OfficialEvidenceValidator(anchors=[])
    decision = v.evaluate(_evidence("https://acme.ai/pricing"))
    assert decision.officiality is not Officiality.OFFICIAL
    assert decision.officiality is Officiality.LIKELY_OFFICIAL


def test_url_shortener_is_third_party() -> None:
    v = _validator(
        [_anchor(domains=["bit.ly"], allow_subdomains=True)]
    )
    decision = v.evaluate(_evidence("https://bit.ly/3xYz"))
    assert decision.officiality is Officiality.THIRD_PARTY
    assert decision.rule_id == RULE_NO_MATCH_THIRD_PARTY
    assert "shortener" in decision.note.lower()


def test_third_party_aggregator_is_third_party() -> None:
    v = _validator()
    decision = v.evaluate(_evidence("https://freeapis.example/pricing"))
    assert decision.officiality is Officiality.THIRD_PARTY
    assert decision.rule_id == RULE_NO_MATCH_THIRD_PARTY


def test_search_result_url_never_official() -> None:
    v = _validator()
    decision = v.evaluate(_evidence("https://search.example/redirect?q=acme.ai"))
    assert decision.officiality is not Officiality.OFFICIAL


def test_validate_appends_rule_note() -> None:
    v = _validator()
    provenance = EvidenceProvenance(
        retrieval_method="safe_fetch",
        original_url="https://acme.ai/pricing",
        final_url="https://acme.ai/pricing",
        http_status=200,
        content_sha256="a" * 64,
        retrieved_from_origin=True,
        retrieved_at=datetime.fromisoformat("2026-09-01T00:00:00+00:00"),
    )
    validated = v.validate(_evidence("https://acme.ai/pricing", provenance=provenance, officiality=Officiality.UNCONFIRMED))
    assert validated.officiality is Officiality.OFFICIAL
    assert any(RULE_EXACT_DOMAIN in n for n in validated.validation_notes)


def test_validator_never_redecides_decided_evidence() -> None:
    v = _validator()
    already_rejected = _evidence("https://acme.ai/pricing", officiality=Officiality.REJECTED)
    assert v.evaluate(already_rejected).officiality is Officiality.REJECTED


def test_third_party_assertion_cannot_create_anchor() -> None:
    """A self-declared 'official' imported URL is not a trust anchor."""
    v = _validator()
    imported = _evidence(
        "https://random-registry.example/acme",
        officiality=Officiality.UNCONFIRMED,
    )
    decision = v.evaluate(imported)
    assert decision.officiality is Officiality.THIRD_PARTY
    # anchors are unchanged by evaluating evidence
    assert [a.provider_id for a in v.anchors] == ["acme", "beta"]


# --- contradictions ---------------------------------------------------------


def _official(evidence_id, source_type, effective=None, published=None, claim="free plan") -> Evidence:
    retrieved = "2026-09-01T00:00:00+00:00"
    provenance = EvidenceProvenance(
        retrieval_method="safe_fetch",
        original_url=f"https://acme.ai/{evidence_id}",
        final_url=f"https://acme.ai/{evidence_id}",
        http_status=200,
        content_sha256="a" * 64,
        retrieved_from_origin=True,
        retrieved_at=datetime.fromisoformat(retrieved),
    )
    return _evidence(
        f"https://acme.ai/{evidence_id}",
        source_type=source_type,
        officiality=Officiality.OFFICIAL,
        provenance=provenance,
        evidence_id=evidence_id,
        effective_at=effective,
        published_at=published,
        claim=claim,
        content_excerpt=claim,
    )


@pytest.mark.parametrize(
    "higher,lower",
    [
        ("pricing", "api-docs"),
        ("pricing", "docs"),
        ("pricing", "blog"),
        ("pricing", "github"),
        ("api-docs", "docs"),
        ("api-docs", "blog"),
        ("api-docs", "github"),
        ("docs", "blog"),
        ("docs", "github"),
        ("blog", "github"),
    ],
)
def test_each_precedence_level(higher: str, lower: str) -> None:
    v = _validator()
    evs = [
        _official("ev-high", higher, effective="2026-01-01T00:00:00+00:00"),
        _official("ev-low", lower, effective="2026-08-01T00:00:00+00:00"),
    ]
    result = v.resolve_contradictions(evs)
    assert result["winner"].evidence_id == "ev-high"


def test_newer_same_priority_wins_by_effective_then_published() -> None:
    v = _validator()
    evs = [
        _official("ev-old", "pricing", effective="2026-01-01T00:00:00+00:00"),
        _official("ev-new", "pricing", effective="2026-08-01T00:00:00+00:00"),
    ]
    assert v.resolve_contradictions(evs)["winner"].evidence_id == "ev-new"

    same_effective = [
        _official("ev-a", "pricing", effective="2026-08-01T00:00:00+00:00", published="2026-01-01T00:00:00+00:00"),
        _official("ev-b", "pricing", effective="2026-08-01T00:00:00+00:00", published="2026-07-01T00:00:00+00:00"),
    ]
    assert v.resolve_contradictions(same_effective)["winner"].evidence_id == "ev-b"


def test_retrieved_at_never_acts_as_policy_date() -> None:
    v = _validator()
    # newer retrieval but older effective date must not win
    evs = [
        _official("ev-stale-policy", "pricing", effective="2025-01-01T00:00:00+00:00"),
        _official("ev-new-policy", "pricing", effective="2026-08-01T00:00:00+00:00"),
    ]
    evs[0].retrieved_at = evs[1].retrieved_at
    assert v.resolve_contradictions(evs)["winner"].evidence_id == "ev-new-policy"


def test_missing_dates_block_confirmation() -> None:
    v = _validator()
    with pytest.raises(ContradictionError):
        v.resolve_contradictions([_official("ev-1", "pricing")], require_dates=True)


def test_ambiguous_case_is_not_required_and_yields_winner() -> None:
    v = _validator()
    result = v.resolve_contradictions([_official("ev-1", "pricing")])
    assert result["winner"].evidence_id == "ev-1"
    assert result["unresolved"] == []


def test_equal_priority_tie_is_unresolved() -> None:
    v = _validator()
    evs = [
        _official("ev-a", "pricing", effective="2026-08-01T00:00:00+00:00", claim="free tier"),
        _official("ev-b", "pricing", effective="2026-08-01T00:00:00+00:00", claim="no free tier"),
    ]
    result = v.resolve_contradictions(evs)
    assert result["unresolved"]


def test_current_pricing_paid_overrides_older_free_blog() -> None:
    v = _validator()
    evs = [
        _official("ev-price", "pricing", effective="2026-08-01T00:00:00+00:00", claim="paid plans only"),
        _official("ev-blog", "blog", effective="2026-01-01T00:00:00+00:00", claim="we are free"),
    ]
    winner = v.current_paid_overrides_older_free(evs)
    assert winner is not None
    assert winner.evidence_id == "ev-price"
    # and the fixed priority also chooses pricing
    assert v.resolve_contradictions(evs)["winner"].evidence_id == "ev-price"


def test_stale_promotion_vs_current_pricing() -> None:
    v = _validator()
    evs = [
        _official("ev-promo", "blog", effective="2025-06-01T00:00:00+00:00", claim="free promotion"),
        _official("ev-price", "pricing", effective="2026-08-01T00:00:00+00:00", claim="subscription required"),
    ]
    assert v.resolve_contradictions(evs)["winner"].evidence_id == "ev-price"
    assert v.current_paid_overrides_older_free(evs).evidence_id == "ev-price"


def test_unresolved_conflict_without_official_evidence() -> None:
    v = _validator()
    with pytest.raises(ContradictionError):
        v.resolve_contradictions(
            [_evidence("https://acme.ai/pricing", officiality=Officiality.LIKELY_OFFICIAL)]
        )


def test_likely_official_never_counted_as_official() -> None:
    v = _validator()
    evs = [
        _evidence(
            "https://docs.acme.ai/pricing",
            officiality=Officiality.LIKELY_OFFICIAL,
        )
    ]
    official = [e for e in evs if e.officiality is Officiality.OFFICIAL]
    assert official == []
    with pytest.raises(ContradictionError):
        v.resolve_contradictions(evs)


def test_same_inputs_produce_one_deterministic_result() -> None:
    v = _validator()
    evs = [
        _official("ev-1", "pricing", effective="2026-01-01T00:00:00+00:00"),
        _official("ev-2", "pricing", effective="2026-08-01T00:00:00+00:00"),
        _official("ev-3", "blog", effective="2026-09-01T00:00:00+00:00"),
    ]
    first = v.resolve_contradictions(evs)
    for _ in range(5):
        assert v.resolve_contradictions(evs)["winner"].evidence_id == first["winner"].evidence_id


def test_priority_order_matches_spec() -> None:
    assert OfficialEvidenceValidator([]).source_priority_order == [
        "pricing",
        "api-docs",
        "docs",
        "blog",
        "github",
    ]


# --- shipped config ---------------------------------------------------------


def test_shipped_config_anchors_use_spec_schema() -> None:
    from hunter.config import DEFAULT_CONFIG_DIR

    anchors = load_trust_anchors(DEFAULT_CONFIG_DIR / "sources.yaml")
    assert len(anchors) >= 5
    by_id = {a.provider_id: a for a in anchors}
    groq = by_id["groq"]
    assert groq.domains == ["groq.com"]
    assert groq.provenance
    assert groq.reviewed_at
    assert match_anchor(groq, "console.groq.com") is True
    assert match_anchor(groq, "groq.com") is True
    assert match_anchor(groq, "github.com", github_repo="groq/groq") is True
    assert match_anchor(groq, "github.com", github_repo="evil/llm") is False
    assert match_anchor(by_id["huggingface"], "huggingface.co.evil.com") is False


# --- TA-008: error-shell pages from anchored domains are not evidence --------


def test_error_shell_from_anchored_domain_is_rejected() -> None:
    ev = _evidence(
        "https://acme.ai/pricing-plans",
        officiality=Officiality.OFFICIAL,
        claim="The author you are looking for could not be found. Author Not Found | Acme",
        content_excerpt="Author Not Found",
    )
    validated = _validator().validate(ev)
    assert validated.officiality is Officiality.REJECTED
    assert any(note.startswith("TA-008:") for note in validated.validation_notes)


def test_substantial_page_mentioning_404_stays_official() -> None:
    ev = _evidence(
        "https://acme.ai/docs/errors",
        officiality=Officiality.OFFICIAL,
        claim="Handling 404 not found responses | Acme API docs",
        content_excerpt="A 404 not found response means the resource does not exist. " * 40,
    )
    assert _validator().validate(ev).officiality is Officiality.OFFICIAL


def test_normal_pricing_page_stays_official() -> None:
    ev = _evidence(
        "https://acme.ai/pricing",
        officiality=Officiality.OFFICIAL,
        claim="Pricing | Acme Free Standard Enterprise",
        content_excerpt="Free plan: 25+ free models with per-model limits. " * 40,
    )
    assert _validator().validate(ev).officiality is Officiality.OFFICIAL


def test_error_shell_rule_only_applies_to_anchored_domains() -> None:
    ev = _evidence(
        "https://unrelated.example.com/pricing",
        officiality=Officiality.OFFICIAL,
        claim="Author Not Found",
        content_excerpt="Author Not Found",
    )
    validated = _validator().validate(ev)
    assert validated.officiality is Officiality.THIRD_PARTY


# --- same-URL snapshots are one source at decision time ----------------------


def test_same_url_snapshots_do_not_conflict() -> None:
    older = _evidence(
        "https://acme.ai/pricing",
        officiality=Officiality.OFFICIAL,
        evidence_id="ev-a",
        claim="Pricing | Acme free plan",
        content_excerpt="A" * 500,
        retrieved_at="2026-09-21T06:00:00+00:00",
    )
    newer = _evidence(
        "https://acme.ai/pricing",
        officiality=Officiality.OFFICIAL,
        evidence_id="ev-b",
        claim="Pricing | Acme free plan",
        content_excerpt="B" * 900,
        retrieved_at="2026-09-21T07:00:00+00:00",
    )
    result = _validator().resolve_contradictions([older, newer])
    assert result["winner"].evidence_id == "ev-b"
    assert result["unresolved"] == []


def test_distinct_urls_same_priority_still_conflict() -> None:
    first = _evidence(
        "https://acme.ai/pricing",
        officiality=Officiality.OFFICIAL,
        evidence_id="ev-a",
        claim="Pricing | Acme free plan",
        content_excerpt="A" * 500,
    )
    second = _evidence(
        "https://acme.ai/pricing-plans",
        officiality=Officiality.OFFICIAL,
        evidence_id="ev-c",
        claim="Plans and pricing | Acme enterprise",
        content_excerpt="C" * 500,
    )
    result = _validator().resolve_contradictions([first, second])
    assert result["unresolved"], "genuinely distinct sources without dates must stay blocked"
