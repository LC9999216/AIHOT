# Phase 3 plan: X / Reddit discovery adapters (remaining-work T5)

**Date:** 2026-09-21
**Authorization:** user directive of 2026-09-21 ("T5 中可在本机做的代码准备（如
X/Reddit 发现适配器代码骨架）"), executed on branch `dsh/remaining-work-execution`
on the Linux server. This is the "新 plan 文档" the remaining-work task spec
requires before any T5 item.
**Authority order:** safety rules → root `AGENTS.md` → `docs/CURRENT_STATUS.md` →
`docs/plans/2026-09-21-remaining-work-task-spec.md` → this document.

## Scope (in)

Code preparation on this machine only:

1. `SourceType` gains two additive discovery values: `reddit`, `x`. No existing
   enum value is renamed or removed.
2. `src/hunter/collectors/reddit.py` — `RedditCollector`, Reddit public JSON
   search endpoint (`https://www.reddit.com/search.json`, credential-free),
   observations only, config-gated `enabled` flag.
3. `src/hunter/collectors/x_twitter.py` — `XCollector`, official X v2 recent
   search endpoint. X has no official free search API, so the adapter is
   disabled unless a bearer token is supplied via the named `X_BEARER_TOKEN`
   environment variable (never committed, never logged — AGENTS.md §13).
4. `config/sources.yaml` discovery section gains `reddit` and `x` stanzas.
5. `hunter discover --source all` wires both adapters behind the existing
   orchestrator (self-disabling collectors, per-collector error isolation).
6. Offline tests in `tests/test_discovery_social.py` (injected transports).

## Contracts honored (AGENTS.md)

- Discovery-only: both adapters create observations/candidates only. They never
  verify, never set `last_verified`, never touch trust anchors, never trigger
  confirmation (§2.1, §7).
- Observation IDs and fingerprints stay deterministic; repeated discovery does
  not duplicate fingerprints (§7).
- Network is injectable; tests are offline and deterministic (§14).
- The X bearer token is optional, read only from its named env var, and is
  never serialized into observations or logs (§13).

## Out of scope (not done here)

- Any live Reddit/X collection run (needs the user's network approval).
- Claude Code client interop verification (`freellmpool==0.13.0` Anthropic
  messages support check — T5 item 2, recorded as pending in CURRENT_STATUS).
- Stage-two gates T2/T3, Feishu delivery, key entry — user-side (Windows live
  environment), marked SKIP in this execution.
