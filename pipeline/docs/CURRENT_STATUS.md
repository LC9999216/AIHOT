# Free Token Hunter current status

Updated: 2026-09-21 (UTC)

This file is the current status source. `PROGRESS.md` records the completed Stage 1 task history;
the older Stage 2 progress files are audit logs and may include historical stop reports.

## Environment

- Execution host for this update: a fresh Linux server
  (`/home/ubuntu/Codex Projects/Free Token Hunter/Free-Token-Hunter`), branch
  `dsh/remaining-work-execution`, venv at `.venv/`.
- The Windows live environment (`D:\AI\Free token\...` worktrees, FlClash, `.env.live`,
  external live-state data dir) is not present on this host. All live-run, FlClash DNS,
  manual signup, and key-entry steps from
  `docs/plans/2026-09-21-remaining-work-task-spec.md` are **SKIP — user-side execution**.

## Code state

- `main` already contains the merge of `codex/live-validation-fixes` (PR #2) and
  `codex/stage2-hardening-fixes` (PR #3), including the four 2026-09-21 fixes
  (`f948f13` localized `icacls` decode, `4d48daa` `.venv` gitignore, `cfcfc9c` TA-008
  soft-404 rejection, `9dd5d41` same-URL snapshot dedup).
- Stage 1 tasks 000–010: implemented. Production grounded extraction remains wired through
  `HUNTER_LLM_BASE_URL` / `HUNTER_LLM_API_KEY` / `HUNTER_LLM_MODEL` with
  `run-stage-one --require-llm`.
- Phase 3 code preparation (remaining-work T5, plan:
  `docs/plans/2026-09-21-phase3-x-reddit-discovery-adapters.md`):
  - `RedditCollector` (`src/hunter/collectors/reddit.py`) — public credential-free Reddit
    JSON search, observations only, config-gated (`discovery.reddit` in `config/sources.yaml`).
  - `XCollector` (`src/hunter/collectors/x_twitter.py`) — official X v2 recent search;
    X has no official free search API, so the adapter stays disabled without
    `X_BEARER_TOKEN`.
  - Both adapters are discovery-only: they never verify providers and never touch trust
    anchors; network is injectable and tests are offline.
- No push was performed from this host; work is committed on
  `dsh/remaining-work-execution` only.

## Fresh offline verification (this host)

- Full suite: `747 passed, 3 skipped` (baseline on the Windows side was identical in count;
  the three skips are the Windows-only `NT ACL path` assertions in
  `tests/test_pool_permissions_encoding.py`, correctly skipped on Linux).
- New Phase 3 tests: `tests/test_discovery_social.py` (7 tests, offline).

## Live-state snapshot (external copy, per the 2026-09-21 task spec)

Recorded from the Windows live run; this host holds no live-state data:

- Providers: AssemblyAI `FREE_CONFIRMED` (conf 85 / free_score 30 / rev 3), Deepgram
  `FREE_CONFIRMED` (conf 85 / 30 / rev 1), OpenRouter `UNCERTAIN` (rev 2, unconfirmed).
- Candidates: 80 (incl. 11 from live HN discovery). Evidence: 105
  (8 OFFICIAL, 94 LIKELY_OFFICIAL, 3 REJECTED).
- Repository-protected `data/` remains the untouched baseline.

## Remaining work (owner, in execution order)

1. **T0** sixth pipeline rerun with `PYTHONUNBUFFERED=1` to diagnose OpenRouter — user-side
   (Windows live env). SKIP here.
2. **T1** FlClash DNS override + evidence collection for the five anchored candidates
   (groq/mistral/huggingface/jina-ai/google-gemini) — user-side. SKIP here.
3. **T2** manual signup, `getpass` key entry, staging health + four conformance canaries —
   user-side only; keys never pass through an agent. SKIP here.
4. **T3** approval binding, promote readback, client request, reverse-verification suspend,
   Feishu delivery — user-side. SKIP here.
5. **T4** merge/publish: branch merges already landed in `main`; worktree cleanup, the
   `v0.2.0-live-validated` tag, and the final live-validation report depend on T2/T3
   outcomes and the Windows worktrees — pending user-side execution.
6. **T5** Reddit/X adapters: code prepared here (see above). Live collection enablement and
   the Claude Code / `freellmpool==0.13.0` Anthropic messages interop check remain open.
7. **T6** recurring stage-one/stage-two scheduling — optional, not started.

No real key, provider request, webhook delivery, promotion, deployment, or push was
performed while producing this status. The project stays offline on this host until the
user-side gates run.
