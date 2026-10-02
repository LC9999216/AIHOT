# Free Token Hunter Specification Revision Implementation Plan

> **For agentic workers:** Execute this plan directly. This is a documentation-only change; do not create product code or initialize Git.

**Goal:** Produce internally consistent stage-one project instructions and Tasks 000-010 for Free Token Hunter.

**Architecture:** Community and imported material produces candidates with preserved observations. Deterministic code validates official evidence, grounds LLM extraction, scores offers, and exclusively controls provider state. The documents define the complete contracts before implementation begins.

**Tech Stack:** Markdown specifications for a future Python 3.12+ project.

---

## Boundaries

- Read-only sources:
  - `C:\Users\HP\OneDrive\桌面\AGENTS.md`
  - `C:\Users\HP\OneDrive\桌面\CODEX_TASKS_000_010.md`
- Outputs:
  - `D:\AI\Free token\Free Token Hunter\AGENTS.md`
  - `D:\AI\Free token\Free Token Hunter\CODEX_TASKS_000_010.md`
- Do not initialize Git, create source code, install dependencies, or implement later milestones.

## Task 1: Revise project-wide instructions

- [ ] Preserve the stage-one scope and security prohibitions.
- [ ] Replace the Candidate/Provider ambiguity with Candidate plus CandidateObservation contracts.
- [ ] Replace the overlapping free-offer enum with orthogonal offer fields.
- [ ] Define trusted-domain anchors, evidence precedence, fetch security, grounded LLM extraction, guarded confirmation, deterministic scoring, and time/idempotency rules.
- [ ] Define registry/history crash-recovery semantics.

## Task 2: Revise Tasks 000-010

- [ ] Make TASK-001 own all shared domain contracts.
- [ ] Make TASK-002 import pinned upstream v2.9.0 data into candidates only.
- [ ] Define candidate and provider persistence without deletion APIs.
- [ ] Prevent generic state transitions from reaching FREE_CONFIRMED.
- [ ] Preserve all discovery provenance across deduplication.
- [ ] Add SSRF controls, trust-anchor validation, field-level extraction grounding, exact V1 scoring, and explicit as-of time.
- [ ] Update task dependencies, tests, and milestone acceptance criteria.

## Task 3: Verification

- [ ] Confirm both desktop source hashes are unchanged.
- [ ] Confirm both output documents are UTF-8 and non-empty.
- [ ] Confirm TASK-000 through TASK-010 each occur exactly once.
- [ ] Confirm Markdown code fences are balanced.
- [ ] Search for obsolete enum and trust-boundary language; allow it only in explicit legacy-mapping notes.
- [ ] Cross-check type names, state semantics, scoring values, file names, and dependency order between both outputs.
- [ ] Report exact output paths and separate completed work from anything unverified.

## Acceptance

- The two output documents can be handed to a coding agent without requiring product-policy decisions.
- Desktop originals remain byte-for-byte unchanged.
- No files outside the three declared outputs are created or modified.
