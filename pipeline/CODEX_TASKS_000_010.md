# CODEX_TASKS_000_010.md

## Free Token Hunter — Codex Development Tasks 000–010

This document defines milestone one:

```text
Discovery observations
  ↓
Candidate aggregation
  ↓
Official evidence resolution and validation
  ↓
Grounded structured extraction
  ↓
Deterministic Verification Confidence and Free Score
  ↓
Guarded Provider Registry update
```

Read `AGENTS.md` before every task. Its public contracts, trust boundary, scoring rules, and security limits are authoritative.

### Explicitly out of scope

Do not implement Feishu, notifications, registration, API-key creation/storage, runtime probes, FreeLLMPool integration, routing, dashboards, PostgreSQL, Redis, queues, or multi-account quota rotation.

All default tests must run offline. Complete and accept tasks in numeric order.

---

# TASK-000 — Project Initialization

## Goal

Create the smallest installable and testable Python 3.12+ repository skeleton.

## Depends On

None.

## Create / Modify

```text
pyproject.toml
README.md
AGENTS.md
.env.example
.gitignore
src/hunter/__init__.py
src/hunter/config.py
src/hunter/cli.py
data/providers.json
data/candidates.json
data/evidence.json
data/history.jsonl
config/settings.yaml
config/sources.yaml
config/scoring.yaml
tests/test_smoke.py
```

## Requirements

1. Configure a `src` package so `hunter` is importable.
2. Provide a console entry point named `hunter` and a version/smoke command.
3. Use `pytest`; add only lightweight formatting, lint, or typing tools that are actually run.
4. Create valid empty JSON collections for providers, candidates, and evidence, plus an empty history JSONL file.
5. Gitignore `.env`, Python caches, build artifacts, and `data/.registry_txn.json`.
6. `.env.example` contains variable names only.
7. README states the milestone-one flow, confirmation meaning, and explicit non-goals.
8. Configuration loading fails with a clear typed error for malformed files.

## Tests

- package import succeeds;
- CLI help/version succeeds;
- all three YAML files load;
- initial JSON/JSONL files are valid;
- `.env` and the registry transaction journal are ignored.

## Acceptance

```text
python -m pip install -e .
python -m pytest
hunter --help
```

All succeed without live credentials or network access.

## Forbidden

No discovery, LLM, database, Docker stack, provider credentials, or future-phase code.

---

# TASK-001 — Shared Domain Contracts

## Goal

Implement the canonical CandidateObservation, Candidate, FreeOffer, Provider, and supporting typed models from `AGENTS.md`.

## Depends On

TASK-000.

## Create / Modify

```text
src/hunter/discovery/models.py
src/hunter/registry/schema.py
tests/test_domain_models.py
tests/fixtures/domain/
data/providers.json
data/candidates.json
```

## Required Types

Implement exactly these enums:

```text
OfferKind: free_tier | free_credit | trial | promotion | unknown
QuotaMode: unmetered | renewing | one_time | unknown
RenewalPeriod: daily | weekly | monthly | custom
AccessMethod: api_key | keyless | oauth | unknown
OfferStatus: active | expired | unknown
ProviderStatus:
  DISCOVERED | EVIDENCE_PENDING | EVIDENCE_VERIFIED | FREE_CONFIRMED |
  UNCERTAIN | NOT_FREE | EXPIRED | REJECTED
```

`renewal_period` is nullable. Unknown booleans and numeric limits are nullable.

Implement `FreeOffer` with these exact public field names:

```text
offer_kind
quota_mode
renewal_period
access_method
offer_status
description
quota_text
expires_at
```

Implement the CandidateObservation, Candidate, FreeOffer, Provider, requirements, API, limits, and score-metadata fields exactly as specified in `AGENTS.md`.

## Validation Rules

1. IDs and fingerprints are non-empty, lowercase, URL-safe strings.
2. Timestamps are timezone-aware ISO 8601 values.
3. Scores are `0..100` or `null`.
4. Unknown numeric limits are `null`, never zero by default.
5. `offer_status=expired` is distinct from `offer_kind`.
6. A Candidate requires at least one observation.
7. Observation fingerprints are unique inside one Candidate.
8. Provider revision is a non-negative integer.
9. `FREE_CONFIRMED` records must contain score metadata and evidence IDs, but full confirmation rules remain in TASK-010.
10. Models reject unknown enum spellings instead of silently coercing them.

## Tests

Cover minimal/complete models, every enum, legacy mapping fixtures, null handling, invalid timestamps and scores, duplicate observations, URL-safe IDs, JSON round trips, and semantic equality independent of dictionary order.

## Acceptance

All later modules can import one canonical representation for each shared concept. No placeholder evidence dictionaries or alternate free-offer enums exist.

## Forbidden

No persistence, fetching, scoring, LLM calls, or state transitions.

---

# TASK-002 — Pinned Seed Importer and Candidate Store

## Goal

Import `pacocartones/free-llm-api-hub` dataset version `2.9.0` into Candidates without inheriting upstream trust decisions.

## Depends On

TASK-001.

## Create / Modify

```text
src/hunter/collectors/registry_seed.py
src/hunter/discovery/store.py
scripts/import_seed.py
src/hunter/cli.py
tests/test_importer.py
tests/test_candidate_store.py
tests/fixtures/upstream/free-llm-api-hub-v2.9.0.json
```

## Input Contract

```text
hunter import-seed --input PATH
```

The importer reads a local JSON file only. It does not download data. Require root `version == "2.9.0"` and a Provider collection compatible with the pinned fixture; reject an incompatible root/major version with a typed error.

## Mapping Rules

For each upstream entry:

1. create a CandidateObservation with `source_type=third_party_registry`;
2. store upstream slug, category, free type, URLs, models, requirements, limits, `verified`, and `last_verified` in normalized observation metadata;
3. convert upstream free-type values to the orthogonal offer-field hints described in the legacy table in `AGENTS.md`;
4. label all mapped values as upstream assertions;
5. never copy upstream `verified` into Provider status or upstream `last_verified` into project `last_verified`;
6. never add imported domains or GitHub organizations to trusted anchors;
7. preserve unknown fields in bounded metadata when useful; never invent values.

## Candidate Store

Use `data/candidates.json` as a complete atomic snapshot:

- stable Candidate ordering by `candidate_id`;
- stable observation ordering by fingerprint;
- exact observation-fingerprint deduplication;
- Candidate merge only through stable ID, exact normalized domain hint, explicit alias, or unambiguous exact normalized name;
- no fuzzy-name auto-merge;
- unchanged input produces byte-identical output and no refreshed timestamps.

Return counts:

```text
candidates_created
candidates_merged
observations_added
unchanged
rejected
errors
```

## Tests

Cover a valid pinned fixture, wrong version, malformed root, missing optional fields, every legacy free-type mapping, duplicate import, updated upstream entry, same Provider with a new observation, ambiguous same-name Providers, provenance retention, stable ordering, atomic replacement, and unchanged timestamps on a no-op.

## Acceptance

Running the same import twice leaves `data/candidates.json` byte-identical. No Provider is created or verified.

## Forbidden

No network fetch, fuzzy merge, Provider verification, runtime probe, secrets, or FreeLLMPool writes.

---

# TASK-003 — Provider Registry and Recoverable History

## Goal

Implement the single Provider persistence boundary, meaningful-change history, and deterministic crash recovery.

## Depends On

TASK-002.

## Create / Modify

```text
src/hunter/registry/store.py
src/hunter/registry/history.py
tests/test_registry.py
data/providers.json
data/history.jsonl
```

## Public Operations

```text
list_providers()
get_provider(provider_id)
find_by_domain(domain)
upsert_provider(provider, reason, source_metadata)
save()
recover_pending_transaction()
```

Do not implement a deletion operation in milestone one.

## Identity and Persistence

Provider matching order is existing stable ID, exact normalized canonical domain, explicit alias, then unambiguous exact normalized name. Never use fuzzy-name auto-merge.

Serialize providers by stable ID. Compare semantic content before writing. A no-op does not increment `revision`, change timestamps, write the registry, or append history.

## History Transaction

For each meaningful update:

1. calculate changed field names and increment Provider revision;
2. derive deterministic `event_id` from Provider ID, revision, and change digest;
3. atomically write `data/.registry_txn.json` containing old/new revisions and the complete event;
4. atomically replace `providers.json`;
5. append the event only if its `event_id` is absent;
6. remove the journal.

On load, reconcile a journal exactly as specified in `AGENTS.md`. Any unexpected third state fails closed.

History contains event ID, timestamp, Provider ID, revision, event type, changed fields, and reason/source metadata. It contains no secrets or page bodies.

## Tests

Cover create/update/no-op, ID/domain/alias lookup, ambiguous names, invalid records, deterministic byte order, atomic replacement, history deduplication, crash before registry replacement, crash after replacement but before history append, successful recovery, and unexpected-revision failure.

## Acceptance

The registry remains the current-state source of truth and an interrupted meaningful update is reconciled without duplicate history events.

## Forbidden

No verification policy, scoring, external calls, deletion, or future-phase state.

---

# TASK-004 — Provider State Machine

## Goal

Enforce ordinary state transitions while reserving confirmation for the final verification policy.

## Depends On

TASK-003.

## Create / Modify

```text
src/hunter/registry/state_machine.py
tests/test_state_machine.py
```

## Ordinary Transition API

```text
transition(provider, new_state, reason, source_metadata)
```

Implement the ordinary transition graph from `AGENTS.md`. The generic API must reject `new_state=FREE_CONFIRMED`, even from `EVIDENCE_VERIFIED`.

`FREE_CONFIRMED` may leave for `EXPIRED` or `UNCERTAIN`. Recovery transitions from `EXPIRED` or `NOT_FREE` return to `EVIDENCE_PENDING` for fresh verification.

Every successful transition persists through the Registry Store and creates one history event. Illegal or failed transitions leave both registry and history unchanged and raise a typed error.

## Tests

Table-test every ordinary allowed transition, representative forbidden transitions, all attempts to enter `FREE_CONFIRMED`, recovery transitions, reason/metadata requirements, history integration, and failure atomicity.

## Acceptance

No caller can reach `FREE_CONFIRMED` through the generic transition API or direct public field mutation.

## Forbidden

No evidence evaluation, scoring, automatic confirmation, keys, probes, or pool states.

---

# TASK-005 — GitHub Candidate Discovery

## Goal

Discover GitHub leads as provenance-preserving CandidateObservations.

## Depends On

TASK-004.

## Create / Modify

```text
src/hunter/collectors/github.py
src/hunter/discovery/deduplicator.py
src/hunter/discovery/orchestrator.py
scripts/discover.py
src/hunter/cli.py
config/sources.yaml
tests/test_discovery.py
```

## Requirements

- Start with configurable repository search.
- Use the default query set from `AGENTS.md` or `config/sources.yaml` for free LLM/API/tier/credit claims.
- Optional GitHub token comes only from a named environment variable.
- Normalize every result into a CandidateObservation and aggregate through the Candidate Store.
- Observation fingerprints use source type, canonicalized result URL, and whitespace-normalized claim.
- Exact duplicate observations no-op; a new source for the same Candidate is retained.
- Repository content is untrusted and is never executed.

CLI:

```text
hunter discover --source github
```

## Tests

Mock all network responses. Cover result normalization, empty results, duplicate suppression, same Candidate/new source, provenance retention, malformed results, pagination bounds, authentication absence, rate limits, transient errors, and deterministic fingerprints.

## Acceptance

A mocked result adds one observation to a Candidate and cannot create or confirm a Provider.

## Forbidden

No issue/discussion/release search unless separately enabled, secret scanning, `.env` search, code execution, officiality, or verification.

---

# TASK-006 — Curated, Hacker News, and Web Search Adapters

## Goal

Run additional discovery adapters behind the same bounded orchestration interface.

## Depends On

TASK-005.

## Create / Modify

```text
src/hunter/collectors/curated_repos.py
src/hunter/collectors/hackernews.py
src/hunter/collectors/web_search.py
src/hunter/discovery/orchestrator.py
config/sources.yaml
tests/test_discovery.py
```

## Adapter Contract

```text
collect(query, run_context) -> list[CandidateObservation]
```

`run_context` carries an explicit run timestamp, limits, and available credentials. Business logic must not depend on one search vendor.

## Requirements

- Curated adapter supports configured immutable source references and prefers structured data over README text.
- HN uses a bounded public search/API and remains discovery-only.
- Web search is disabled cleanly when credentials are absent.
- Orchestrator runs enabled collectors, isolates failures, aggregates observations, persists once, and returns per-collector and total counts.
- Cross-source matches preserve every observation rather than choosing one provenance record.
- Collector and query limits come from configuration; no recursive crawl.

## Tests

Mock every adapter. Cover one failure while others succeed, disabled and credential-missing adapters, structured curated input, cross-source aggregation, same URL/different claim, same claim/different URL, deterministic output, and bounded calls.

## Acceptance

One run can aggregate multiple sources into stable Candidates without losing provenance or changing Provider state.

## Forbidden

No official verification, recursive crawling, LLM decisions, fuzzy identity merges, or future-phase actions.

---

# TASK-007 — Evidence Model, Store, Resolver, and Safe Fetcher

## Goal

Resolve and safely fetch bounded public evidence while preserving provenance.

## Depends On

TASK-006.

## Create / Modify

```text
src/hunter/evidence/models.py
src/hunter/evidence/store.py
src/hunter/evidence/resolver.py
src/hunter/evidence/fetcher.py
data/evidence.json
tests/test_evidence.py
tests/fixtures/evidence/
```

## Evidence Contract

Implement the exact Evidence fields and officiality enum from `AGENTS.md`. New evidence starts `UNCONFIRMED`; the resolver/fetcher never marks it `OFFICIAL`.

Persist Evidence atomically in stable `evidence_id` order. Derive identity from canonical URL plus content fingerprint. Re-fetching identical content must not duplicate evidence.

## Resolver

Use Candidate observations, normalized domain hints, configured URL templates, and search leads to propose a bounded list of homepage, pricing, API docs, developer docs, free-tier docs, and official-announcement URLs. A high search rank is not officiality.

## Fetcher Security

Implement these exact limits and checks:

- allow HTTP/HTTPS only and ports 80/443 only;
- reject URL user-info credentials;
- resolve the hostname before connecting;
- require every IPv4/IPv6 result to be public and reject loopback, private, link-local, reserved, multicast, and unspecified addresses;
- revalidate DNS and the complete policy on every redirect;
- allow maximum three redirects;
- allow maximum 2 MiB response body;
- apply a 10-second total request timeout;
- accept only bounded text/HTML, plain text, or JSON content;
- fetch maximum six pages per Provider per run.

Do not forward credentials, cookies, or authorization headers between requests.

## Tests

Cover canonical URL normalization, duplicate content, exact and unrelated domains, accepted content, unsupported types, declared and streamed oversize bodies, timeout, redirect limit, user-info URLs, disallowed ports, IPv4/IPv6 loopback/private/link-local/reserved/multicast/unspecified targets, DNS resolving to private space, public-to-private redirect, and bounded page counts.

## Acceptance

The system can persist normalized unconfirmed evidence without connecting to a non-public target or assigning trust.

## Forbidden

No scoring, LLM extraction, officiality decision, authenticated browsing, JavaScript execution, or browser login.

---

# TASK-008 — Official Evidence Validator and Contradiction Policy

## Goal

Apply deterministic trust anchors and evidence precedence to produce officiality and contradiction results.

## Depends On

TASK-007.

## Create / Modify

```text
src/hunter/evidence/validator.py
src/hunter/evidence/resolver.py
config/sources.yaml
tests/test_evidence_validator.py
```

## Trust Anchors

`config/sources.yaml` entries must contain:

```text
provider_id
domains[]
allow_subdomains
github_organizations[]
github_repositories[]
provenance
reviewed_at
```

Imported/search/model output never writes these entries.

Normalize hosts with lowercase, trailing-dot removal, and IDNA ASCII conversion. Match exact labels. Accept a subdomain only when the matching anchor explicitly allows it. GitHub sources require an exact organization/repository mapping.

## Result Contract

Return `OFFICIAL`, `LIKELY_OFFICIAL`, `UNCONFIRMED`, `THIRD_PARTY`, or `REJECTED`, plus rule IDs and notes. Only `OFFICIAL` may support confirmation.

## Contradictions

Implement the fixed priority from `AGENTS.md`. Within equal priority, compare `effective_at`, then `published_at`. Never use `retrieved_at` as the policy date.

If dates are absent, equal-priority claims conflict, or higher-priority content is ambiguous, return an unresolved contradiction. A current pricing page saying paid overrides an older official blog saying free.

## Tests

Cover exact domain, permitted and prohibited subdomains, trailing-dot and IDNA normalization, lookalike domains, URL shorteners, imported URL without anchor, exact GitHub mapping, fork/username similarity, each precedence level, newer same-priority evidence, missing dates, stale promotion/current pricing, unresolved conflicts, and proof that `LIKELY_OFFICIAL` never counts as official.

## Acceptance

Third-party assertions cannot establish their own trust anchor, and contradictions have one deterministic result for the same inputs.

## Forbidden

No LLM officiality, popularity/ranking trust, automatic anchor creation, scoring, or Provider confirmation.

---

# TASK-009 — Grounded LLM Structured Extractor

## Goal

Extract normalized offer facts while proving every accepted non-null field is supported by supplied evidence.

## Depends On

TASK-008.

## Create / Modify

```text
src/hunter/llm/client.py
src/hunter/llm/prompts.py
src/hunter/llm/structured_extractor.py
src/hunter/evidence/extractor.py
tests/test_llm_extractor.py
tests/fixtures/llm/
```

## Output Contract

Return Candidate/Provider name hints, every FreeOffer fact, quota numbers/text, requirements, commercial/regional conditions, API fields, models, uncertain fields, and a FieldEvidenceReference for every non-null fact:

```text
field
evidence_id
quote
start_offset
end_offset
```

The deterministic grounding validator requires valid offsets and an exact quote match in the supplied evidence text. A valid-schema value without valid grounding is rejected or set to `null` according to field policy.

## LLM Boundary

Use a replaceable structured-client interface. The client receives messages, schema, and evidence text only; it receives no tools, environment variables, files, or arbitrary network access.

The prompt explicitly treats page instructions as untrusted content, requires supported facts only, preserves unknowns, and denies authority over officiality, contradictions, scores, and state.

Allow one repair attempt for malformed output. Record failure and keep the Candidate retryable.

## Tests

Use only fake clients. Cover valid grounded output, missing fields, malformed JSON, unknown enum, extra fields, invalid offsets, mismatched quote, schema-valid hallucinated value, prompt injection, conflicting statements, expired promotion, secret-like source text, single repair success, and repair-bound failure. Assert that no tool definitions or secrets are passed to the client.

## Acceptance

A mocked page produces validated structured facts only when each accepted value is grounded in the supplied text. The extractor cannot update Provider state.

## Forbidden

No LLM scores, officiality, state mutation, tool calls, secret transmission, or silent coercion.

---

# TASK-010 — V1 Scoring, Confirmation Guard, and Stage-One Pipeline

## Goal

Implement the exact V1 scores, the exclusive confirmation service, and a rerunnable stage-one pipeline.

## Depends On

TASK-009 and every previous task.

## Create / Modify

```text
src/hunter/scoring/confidence.py
src/hunter/scoring/free_score.py
src/hunter/registry/confirmation.py
src/hunter/discovery/orchestrator.py
src/hunter/cli.py
config/scoring.yaml
tests/test_scoring.py
tests/test_stage_one.py
```

## Score Contract

```text
score(normalized_input, scoring_config, as_of) -> ScoreResult

ScoreResult:
  score
  score_version = "v1"
  as_of
  config_digest
  breakdown
```

`as_of` is required and timezone-aware. Scoring code never reads the system clock or uses randomness.

## Verification Confidence V1

Implement exactly:

- base from the highest-priority supporting `OFFICIAL` source: pricing 45, API/developer docs 40, product docs 35, official blog 25, mapped official GitHub 20;
- second consistent independent official source: +10 once;
- explicit free/no-cost wording: +20;
- explicit programmatic API applicability: +15;
- newest supporting retrieval at most 90 days old: +10;
- all critical fields grounded: +5;
- ambiguous free/API wording: -20;
- evidence older than 180 days: -20;
- missing usable evidence date: -10;
- unresolved contradiction: -50 and hard confirmation block;
- clamp to 0..100;
- confirmation threshold: 80.

Critical fields are offer kind, active/expiry facts, programmatic API applicability, and the free quota/description that supports the claim.

## Free Score V1

If expired, return 0. Otherwise sum:

- ongoing/no known end +25; expiry >90 days +15; 31-90 days +10; 8-30 days +5; <=7 days +0;
- unmetered +25; renewing +20; one-time +5;
- keyless +10;
- no card +10 / card required -10;
- no phone +5 / phone required -5;
- OpenAI compatible +10;
- current grounded model list +5;
- documented limits +5;
- commercial allowed +10 / forbidden -15;
- material regional restriction -10;
- unknown values 0;
- clamp to 0..100.

`config/scoring.yaml` contains these exact values and `score_version: v1`. Store a deterministic configuration digest and complete breakdown with the Provider.

## Exclusive Confirmation

```text
confirm_provider(provider, evidence_result, extraction, scores, as_of, reason)
```

`confirm_provider()` is the only function allowed to perform `EVIDENCE_VERIFIED -> FREE_CONFIRMED`. It checks every hard gate in `AGENTS.md`, including at least one `OFFICIAL` item, active offer, grounded critical fields, explicit programmatic API scope, known promotion expiry, no unresolved contradiction, and confidence >=80. Failure returns a typed decision and does not mutate state.

## Pipeline

Add one canonical command:

```text
hunter run-stage-one --as-of ISO_TIMESTAMP
```

Flow:

```text
load Candidates
→ resolve/fetch Evidence
→ validate officiality and contradictions
→ grounded extraction
→ normalize Provider
→ calculate both scores
→ ordinary state transitions
→ guarded confirmation when eligible
→ registry/history transaction
→ summary
```

Summary fields:

```text
candidates_processed
providers_created
providers_updated
free_confirmed
uncertain
not_free
expired
rejected
unchanged
errors
```

One Candidate failure must not corrupt other results. Repeating the same fixtures, configuration, and `as_of` produces byte-identical registries and no duplicate history.

## Tests

Table-test every scoring factor, unknown neutrality, date boundaries at 7/8/30/31/90/91/180/181 days, clamp boundaries, configuration digest, identical-input determinism, and expired score zero.

Confirmation tests cover threshold 79/80, third-party-only evidence, `LIKELY_OFFICIAL`, missing promotion expiry, missing grounding, consumer-chat-only offers, unresolved contradiction, current paid pricing, and successful confirmation.

Add one fully offline end-to-end fixture test from Candidate observations through registry/history, plus a second identical run proving byte-level idempotency.

## Acceptance

- Strong, current, grounded first-party evidence may reach `FREE_CONFIRMED`.
- Community or imported assertions alone never reach it, regardless of LLM output or score.
- Expired/contradicted promotions do not remain confirmed.
- Scores expose reproducible V1 breakdowns.
- All tests pass without live services.

## Forbidden

No Feishu, credentials, registration, probes, FreeLLMPool, routing, dashboards, LLM-decided scores, hidden clock reads, or later-milestone work.

---

# Required Execution Order

```text
TASK-000
→ TASK-001
→ TASK-002
→ TASK-003
→ TASK-004
→ TASK-005
→ TASK-006
→ TASK-007
→ TASK-008
→ TASK-009
→ TASK-010
```

Do not parallelize tasks that change shared contracts before the lower-numbered task is accepted.

---

# Milestone-One Final Acceptance Checklist

- [ ] Package installs and CLI help runs.
- [ ] Shared domain models and orthogonal offer fields validate.
- [ ] Pinned seed data imports as Candidates only.
- [ ] All cross-source observations are preserved and deduplicated.
- [ ] Candidate, Evidence, and Provider snapshots are deterministic.
- [ ] Registry/history interruption is recoverable.
- [ ] Generic transitions cannot enter `FREE_CONFIRMED`.
- [ ] GitHub, curated, HN, and web adapters create observations only.
- [ ] Fetcher blocks direct and redirect-based SSRF.
- [ ] Officiality requires a configured trust anchor.
- [ ] `LIKELY_OFFICIAL` and third-party evidence cannot confirm.
- [ ] Evidence contradictions use fixed priority/date rules.
- [ ] Every accepted LLM field has a verified evidence reference.
- [ ] Prompt injection cannot call tools or change decisions.
- [ ] V1 scoring uses fixed weights and explicit `as_of`.
- [ ] Confirmation enforces threshold and every hard gate.
- [ ] Repeated fixture runs produce no duplicate records or no-op history.
- [ ] Complete automated suite passes offline.
- [ ] No Feishu, secret, registration, probe, pool, routing, dashboard, database, or queue code exists.

Stop at this milestone until a new task explicitly authorizes later work.
