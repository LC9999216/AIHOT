# Free Token Hunter — Progress

**Goal:** Stage one discovers free AI API leads, confirms them against configured first-party
evidence, grounds LLM extraction, scores deterministically, and writes a rerunnable Provider Registry.

**Task order:** TASK-000 → 001 → 002 → 003 → 004 → 005 → 006 → 007 → 008 → 009 → 010 (strictly serial).

**Max risk:** Frozen spec hashes (`AGENTS.md`, `CODEX_TASKS_000_010.md`) must stay byte-identical
while public contracts are implemented; any deviation invalidates delivery.

**Baseline (verified 2026-09-11):** workspace held exactly `AGENTS.md`, `CODEX_TASKS_000_010.md`,
`docs/superpowers/plans/2026-09-11-revise-free-token-hunter-specs.md`; no `.git`; both spec hashes
matched the task book. Python 3.12.10, pytest 9.0.2, pydantic 2.12.5, PyYAML 6.0.3 preinstalled.

**Note (simpler option adopted):** upstream v2.9.0 tag fixture was fetched once over the configured
HTTPS proxy and frozen at `tests/fixtures/upstream/free-llm-api-hub-v2.9.0.json`
(SHA256 `C3753C5F…A5A9E`, 74845 bytes, 69 providers); tests stay fully offline afterwards.

---

## Task log

| Task | Status | Notes |
|---|---|---|
| TASK-000 | completed | Skeleton + config + CLI + data files; 17/17 smoke tests green; `pip install -e .`, `hunter --help`, `python -m pytest` all pass. |
| TASK-001 | completed | Canonical models + enums + legacy mapping; 38 tests green; full suite 55 passed. |
| TASK-002 | completed | Seed importer (v2.9.0, 69 entries) + Candidate Store; re-import byte-identical; no Provider created; full suite 84 passed. |
| TASK-003 | completed | Provider Registry + journaled History + crash recovery; 21 registry tests green; full suite 105 passed. |
| TASK-004 | completed | State machine with exclusive FREE_CONFIRMED guard; 29 tests green; reverse verification proved generic transition raises TransitionError; full suite 134 passed. |
| TASK-005 | completed | GitHub collector + deduplicator + orchestrator; 15 discovery tests green; full suite 149 passed; `hunter discover --help` works. |
| TASK-006 | completed | Curated + HN + Web Search adapters; cross-source aggregation preserves all observations; disabled web search without keys; full suite 161 passed. |
| TASK-007 | completed | Evidence model/store/resolver + SafeFetcher with SSRF controls (direct, IPv6, public→private redirect, timeout, size, page bound); reverse verification proved private fetch rejected; full suite 204 passed. |
| TASK-008 | completed | Trust anchor loader + OfficialEvidenceValidator; only anchored evidence OFFICIAL, LIKELY_OFFICIAL never upgrades, lookalike/homograph domains fail; contradiction priority+date rules; 10 anchors added to config; full suite 226 passed. |
| TASK-009 | completed | Tool-less grounded LLM extractor split into spec layout (`llm/client.py`, `llm/prompts.py`, `llm/structured_extractor.py`, `evidence/extractor.py`) + `tests/fixtures/llm/`; quote/offset exact-match, unknown enums rejected without silent coercion, extra fields dropped, ≤1 repair, no tools/secrets to the client; full suite 312 passed. |
| TASK-010 | completed | Scoring V1 split into `scoring/confidence.py` + `scoring/free_score.py`; `confirm_provider()` hard gates incl. explicit programmatic-API scope; canonical CLI `hunter run-stage-one --as-of ISO_TIMESTAMP` emitting the spec summary fields; `tests/test_stage_one.py` + `tests/fixtures/evidence/`; full suite **354 passed, 0 failed, 0 errors, 0 skipped**. |
| Defect fix (evidence persistence) | `EvidenceStore.upsert` only saved on the insert path, so validator officiality changes stayed in memory and reached disk only order-dependently. Merge path now persists real changes and skips no-op rewrites; a re-fetch also no longer downgrades a decided `officiality` back to `UNCONFIRMED`, and `validate()` is idempotent (no duplicated rule notes). All four `data/` files are now byte-identical across three consecutive runs. |
| TASK-008 (rev) | completed | Anchor schema aligned to the spec's pinned names (`domains[]`, `github_organizations[]`, `github_repositories[]`, `reviewed_at`) with rule IDs + notes; tests moved to the spec path `tests/test_evidence_validator.py` (43 tests). |
| TASK-007 (rev) | completed | Added `tests/fixtures/evidence/` (official, ambiguous, third-party, contradictory, stale, SSRF target list) and fixture-driven SSRF tests. |

---

## Adoption notes (simpler options, recorded per task book)

- **TASK-007 fixtures**: the SSRF fixture splits targets by the check that catches them —
  `rejected_static` (scheme/port/user-info/IP-literal, caught by `check_url` + `check_host_addresses`)
  vs `rejected_dns` (clean-looking hostnames resolving into private space, caught by the pre-connect
  DNS step). This matches the real fetcher order instead of asserting one function catches both.
- **TASK-009 client boundary**: `assert_no_tools` inspects only the request structure, because
  untrusted *page text* may legitimately contain strings like `api_key=`. Outbound credential
  protection is `assert_no_secrets` against known secret values — precise, no false positives.
- **TASK-010 API scope**: the programmatic-API-scope hard gate means the two fixture providers whose
  evidence only describes consumer surfaces (`deepgram`, `huggingface`) no longer confirm. `data/` was
  regenerated so the persisted registry reflects current hard gates rather than pre-gate state (§6
  "illegal or insufficient transitions fail closed").

---

## Final verification (TASK-010 acceptance)

| Check | Result |
|---|---|
| `python -m pip install -e .` | success |
| `python -m hunter --help` | exit 0; lists `run-stage-one` (+`pipeline` alias) |
| Full `python -m pytest` | **354 passed, 0 failed, 0 errors, 0 skipped** |
| Two-run idempotency (`data/`) | providers.json SHA256 identical both runs; history.jsonl line count 2 → 2 (no new no-op events) |
| Three-run byte identity (all data files) | `candidates.json`, `evidence.json`, `providers.json`, `history.jsonl` all byte-identical across runs 1, 2, and 3 at the same `as_of` |
| Fresh-directory determinism | run 1 == run 2 byte-identical; fresh-dir SHA256 == `data/` SHA256 `A8565891…B31670` |
| Reverse verification (FREE_CONFIRMED) | temporary test asserting the generic transition API reaches FREE_CONFIRMED **failed as intended**: `TransitionError: FREE_CONFIRMED cannot be reached through the generic transition API; use confirm_provider()`; temp test removed, suite green |
| Reverse verification (SSRF) | temporary tests asserting private/loopback/link-local targets are public and fetchable **failed as intended**: `assert False is True` for `10.1.2.3`/`127.0.0.1`/`169.254.169.254`/`::1`, and `FetcherError: host 'internal.local' resolves to non-public addresses`; temp tests removed, suite green |
| `AGENTS.md` SHA256 | `90E744D0CCBEE584F2329FB5CBCEFB7CAE0584FE2EDD61599701FF1BDCBA18E3` (unchanged) |
| `CODEX_TASKS_000_010.md` SHA256 | `7EC88D85946C8A54A6EDFEE45F85632C1D9FEAA23A216669439BD96A749C2DEE` (unchanged) |
| Credential scan | no real credentials in the repo or outputs (only placeholder/example markers) |

