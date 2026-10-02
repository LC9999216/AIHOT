# Phase 4 progress — client-config authority, routing, and fail-closed readback

Plan: `docs/plans/2026-09-17-stage2-live-validation-remediation.md` §五.阶段 4.
Worktree `D:\AI\Free token\Free Token Hunter-stage2-live-validation-remediation`, branch
`codex/stage2-live-validation-remediation`. All commands ran with explicit workdir and
`$env:PYTHONPATH='src'`. Committed as `70dfa1c fix(client-config): generate configs from loaded pool catalog`.

## Verified interface facts (pre-implementation)

- Codex current `main` tree SHA `5e636ea760d1c821a9162af652a14225fdbd8a0f`
  (https://api.github.com/repos/openai/codex/git/trees/main?recursive=1).
- https://raw.githubusercontent.com/openai/codex/main/codex-rs/core/config.schema.json —
  `ModelProviderInfo`: `base_url` (string), `env_key` = "Environment variable that stores the
  user's API key", `wire_api` enum has exactly `"responses"` ("The Responses API exposed by
  OpenAI at `/v1/responses`"); `Profile` carries `model` + `model_provider`.
- https://raw.githubusercontent.com/openai/codex/main/codex-rs/model-provider-info/src/lib.rs —
  `WireApi::Responses` only; `chat` wire API is a deserialize error ("CHAT_WIRE_API_REMOVED");
  `api_key()` reads `std::env::var(env_key)`; base_url is joined with endpoint paths.
- https://raw.githubusercontent.com/openai/codex/main/codex-rs/codex-api/src/provider.rs —
  `url_for_path` trims trailing `/` from base and leading `/` from path.
- https://raw.githubusercontent.com/openai/codex/main/codex-rs/codex-api/src/endpoint/responses.rs —
  endpoint POSTs `/responses`, so `base_url` MUST be the standard `/v1` root and must NOT end
  in `/responses` (the old `/v1/{provider_id}/responses` shape is both fictional and would
  double-append).
- https://opencode.ai/docs/providers/ — custom provider: `npm: "@ai-sdk/openai-compatible"`,
  `options.baseURL` (API endpoint), `options.apiKey: "{env:NAME}"` environment-reference
  syntax, and `models` keyed by REAL served model IDs (never invented `<provider>-default`).
- Installed `freellmpool==0.13.0`
  (`C:\Users\HP\AppData\Local\Programs\Python\Python312\Lib\site-packages\freellmpool`):
  - `proxy.py` L612–635: standard routes `/v1/responses`, `/v1/chat/completions`, … and Bearer
    proxy auth checked BEFORE body parsing → route-level verification is possible offline with
    a no-auth 401 without any upstream call.
  - `proxy.py` L83–94 `_model_ids` → enabled model IDs are `f"{provider.id}/{m.name}"`;
    L97+ `/v1/models` payload; L1755–1775 `_parse_model` pins `provider/model` when the first
    segment is a real provider id.
  - Catalog/env facts already encoded in repo: `FREELLMPOOL_CONFIG` providers.toml (no keys),
    `FREELLMPOOL_CONFIG_FILE` config.toml `[keys]`; `pool_service.py` already authenticates the
    proxy with `FREELLMPOOL_PROXY_KEY` — adopted as the single proxy auth env NAME.

## Implemented behavior (owned files)

- `src/hunter/client_config.py`: strict `validate_production_catalog` (exact 4-key allowlist,
  0.13.0 version gate, loopback `/v1` base URL, `FREELLMPOOL_PROXY_KEY` auth env NAME, provider
  id charset, `provider/model` pins, secret scan); Codex TOML (single `[model_providers.freellmpool]`,
  standard `base_url`, `wire_api="responses"`, `env_key`, per-model `[profiles."<id>"]`);
  OpenCode JSON (`options.baseURL` + `options.apiKey="{env:FREELLMPOOL_PROXY_KEY}"`, real model
  IDs, `model` default); agent YAML (single endpoint + `auth_env` NAME + model list). No
  provider-ID path segments anywhere; provider display names are no longer copied into configs.
  Writer: all three documents generated + re-parsed + secret-scanned BEFORE any output
  directory is created, then staged (temp file + fsync) and `os.replace`d per file; a failed
  replace rolls back already-replaced files (existing content restored, new files removed).
  Documented limitation: fixed independent filenames cannot give simultaneous visibility /
  power-loss atomicity across the set; consumers must not reload mid-update.
- `src/hunter/pool_proxy.py`: `ProxySupervisor.production_catalog()` — strict live readback of
  the LOADED pool only (enabled `provider/model` IDs from `server.pool.providers`, actual bound
  host:port, proxy key required, thread liveness + identity re-check, `check_version()`), never
  from TOML on disk.
- `src/hunter/pool_control.py`: `production_catalog()` — fails closed when halted, when no live
  proxy supervisor exists (disk TOML is NOT a fallback, unlike `list_production`), on version
  mismatch, or schema invalid; wraps all failures as `PoolControlError("production_catalog_unavailable")`.
- `src/hunter/pool_api.py`: authenticated `GET /pool/catalog` (409 sanitized on unavailable) and
  `PoolControlClient.production_catalog()` (schema-invalid response → `invalid_production_catalog`).
- `src/hunter/cli_imports.py`: `client-config write` now REQUIRES `HUNTER_POOL_CONTROL_URL` +
  `HUNTER_POOL_CONTROL_TOKEN`, fetches the catalog readback, and exits 2 when unconfigured /
  1 on readback or write failure (sanitized error JSON, no paths/keys echoed). The `None`
  fallback that previously generated production configs from runtime state alone is removed.
- Tests: `tests/test_client_config_catalog.py` (new, offline fixtures + loopback fixture
  servers only) and `tests/test_stage5.py` (migrated to explicit catalog fixtures and the
  verified /v1 contract). No real credentials, webhooks, services, or live calls; both HTTP
  fixture servers are loopback and shut down.

## Exact RED/GREEN commands and counts

- Initial clean RED (new regression file, after fixing fixture cleanup only):
  `python -B -m pytest -q tests/test_client_config_catalog.py --tb=no`
  → **21 failed in 1.27s**.
- Priority RED→GREEN (missing readback must exit nonzero and leave all three outputs
  untouched, nonexistent + pre-existing bytes preserved):
  `python -B -m pytest -q tests/test_client_config_catalog.py -k cli_missing --tb=no`
  → RED **6 failed, 18 deselected in 0.12s** (assertion `_handle_client_config_write(args) != 0`
  failed; legacy handler returned 0 / then raised uncaught `ClientConfigError`);
  → GREEN **6 passed, 18 deselected in 0.11s**.
- After wiring the catalog endpoint:
  `python -B -m pytest -q tests/test_client_config_catalog.py tests/test_stage5.py tests/test_pool_proxy.py --tb=short`
  → 15 failed (1 order-sensitive fixture assertion + 14 legacy tests asserting the old
  per-provider URLs / `confirmed_production_ids` contract), 46 passed.
- Final focused GREEN:
  `python -B -m pytest -q tests/test_stage5.py tests/test_client_config_catalog.py --tb=short`
  → **49 passed in 1.51s** (reproduced 1.55s / 1.51s).
- Parent runner file incl. migrated fail-closed CLI test:
  `python -B -m pytest -q tests/test_stage2_runner.py --tb=short` → **42 passed in 13.28s**;
  combined single-command check with owned suites → **62 passed in 1.90s**.
- Full-suite background run (`python -B -m pytest -q --tb=long`, 305s) ran WHILE the parent was
  editing `runtime/stage2.py` / `test_stage2_runner.py`: 681 passed, 1 skipped, and 14 failures
  that were artifacts of mid-edit collection (12 in `test_stage5.py`, all pass focused) plus the
  parent's two mid-fix runner tests (now green). Post-stop isolated `test_stage_one.py` runs
  timed out under the same concurrent-edit contention; `candidates.json` behavior is untouched
  by phase-4 files and is deferred to the phase-7 full-suite gate. No further docs browsing.

## Not done / boundaries

- No live FreeLLMPool, credentials, webhooks, or external live-state touched; no push / PR /
  merge / deploy; no history rewrite; protected data and `AGENTS.md` untouched.
- `runtime/stage2.py` and the HIGH containment tests belong to the parent; my commit stages
  only the seven files listed in `70dfa1c`.
- Offline route check uses real `freellmpool.proxy.serve` fixture: generated
  `/v1/responses` + `/v1/chat/completions` return 401 (route matched, auth refused) with an
  unauthenticated fixture pool — parse-only validation was explicitly not relied upon.
