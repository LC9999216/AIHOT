# Free Token Hunter

Current implementation and live-gate status: [`docs/CURRENT_STATUS.md`](docs/CURRENT_STATUS.md).

Free Token Hunter discovers legitimate free AI/LLM API offers, verifies them against current
first-party evidence, extracts normalized facts, calculates deterministic trust/value scores, and
writes a local provider registry.

## Milestone-one flow

```text
Internet / GitHub / curated sources
        ↓
Candidate + source observations
        ↓
Official evidence resolution and validation
        ↓
Grounded structured extraction
        ↓
Verification Confidence
        ↓
Free Score
        ↓
Provider Registry
```

## What "confirmed" means in stage one

Stage-one confirmation means **currently documented by sufficient first-party evidence as a free
programmatic API offer**. It does **not** prove that a real credential or request has worked. No
registration, credential creation, or runtime API probing happens here.

Community sources (GitHub, Hacker News, curated lists, web search, imported registries) create or
enrich a **Candidate** only. Only first-party evidence validated against configured trust anchors
can lead to `FREE_CONFIRMED`. Imported `verified`/`last_verified` values are treated as third-party
assertions, preserved as observation metadata, and never copied into project state.

## Scoring

- **Verification Confidence V1** measures how strongly current, grounded, first-party evidence
  supports a free programmatic API offer.
- **Free Score V1** measures how attractive the free offer is.
Both are deterministic, use fixed weights from `config/scoring.yaml`, and require an explicit
timezone-aware `as_of` timestamp. They never read the system clock.

## Explicit non-goals (stage one)

- No Feishu synchronization or notifications.
- No provider account registration.
- No API-key creation or storage (credentials appear only as named variables in `.env.example`).
- No runtime API probing.
- No FreeLLMPool integration.
- No load balancing or provider routing.
- No dashboards.
- No multi-account quota rotation.
- No PostgreSQL, Redis, queues, or distributed workers.

## Repository layout

```text
src/hunter/
  collectors/     discovery adapters and the pinned seed importer
  discovery/      observations, candidate store, deduplicator, orchestrator
  evidence/       models, store, resolver, safe fetcher, validator, extractor
  registry/       schema, store, history, state machine, confirmation
  scoring/        Verification Confidence V1 and Free Score V1
  llm/            tool-less grounded extraction boundary
  config.py       configuration loading
  cli.py          command-line interface
data/             providers.json, candidates.json, evidence.json, history.jsonl
config/           settings.yaml, sources.yaml, scoring.yaml
tests/            fully offline deterministic suite
```

## Commands

```text
hunter version
hunter import-seed --input PATH
hunter discover --source github
hunter collect-evidence --candidate-id ID
hunter run-stage-one --as-of ISO_TIMESTAMP   # alias: hunter pipeline
```

`run-stage-one` resolves and fetches public evidence before validation.  Without a configured
real LLM extractor it fails closed at the pending/uncertain boundary and never confirms a
Provider.  The automated fixture tests inject deterministic fake fetcher and extractor
boundaries and print the stage-one summary:

```text
candidates_processed, providers_created, providers_updated, free_confirmed,
uncertain, not_free, expired, rejected, unchanged, errors
```

For a real Stage 1 run, configure an OpenAI-compatible Chat Completions endpoint through process
environment variables. The API key is sent only as an HTTP Authorization header and is never put
in prompts, logs, registry data, or command-line arguments:

```powershell
$env:HUNTER_LLM_BASE_URL = "https://YOUR-ENDPOINT.example/v1"
$env:HUNTER_LLM_API_KEY = "YOUR-KEY"
$env:HUNTER_LLM_MODEL = "YOUR-JSON-CAPABLE-MODEL"
python -m hunter run-stage-one --require-llm --data-dir "D:\path\to\external-stage1-data"
```

Use `--require-llm` for unattended or release runs. It exits with code 2 when the real grounded
extractor is absent or only partially configured, preventing a fail-closed no-op from being
mistaken for a successful verification run. Keep live data outside the repository and never place
real values in `.env.example`.

## Confirmation hard gates

`confirm_provider()` is the only path into `FREE_CONFIRMED`; the generic transition API rejects it.
All of these must hold:

1. a stable Provider identity;
2. at least one `OFFICIAL` item supporting a current free **programmatic API** offer
   (a free consumer chat UI does not qualify);
3. grounded structured extraction (quote + offsets matching the evidence text exactly);
4. normalized offer fields, with `offer_status` decided by code from `as_of`;
5. no unresolved higher-priority contradiction;
6. `offer_status=active`;
7. a known expiry for promotions;
8. Verification Confidence at or above 80.

Any failure returns a typed error and mutates nothing.

## Running the tests

```bash
python -m pip install -e .
python -m pytest          # fully offline: network, GitHub, search, and LLM are all fakes
```

Default tests never hit the network and never mock business rules — only the network/LLM boundary is
faked. The pinned upstream fixture (`tests/fixtures/upstream/free-llm-api-hub-v2.9.0.json`) is frozen
in the repo so every run is reproducible.

See `CODEX_TASKS_000_010.md` for the authoritative task breakdown.

## Optional Stage 2 Pool Control service

Stage 2 is separate from the Stage 1 confirmation pipeline. Install its exact
runtime dependency in the isolated Pool Control environment:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev,stage2]"
.\.venv\Scripts\python.exe -c "import freellmpool; assert freellmpool.__version__ == '0.13.0'"
```

1. Set the named environment variables in the relevant local process
   environments without recording their values: `HUNTER_POOL_CONTROL_TOKEN`,
   `HUNTER_POOL_CONTROL_URL`, and `FREELLMPOOL_PROXY_KEY`.
2. Start `hunter-pool-control serve` in the isolated Pool Control environment.
3. Run `python -m hunter run-stage-two` in the Hunter environment.
4. Treat every nonzero exit code as fail-closed and review it before retrying.

The Pool Control service owns the provider TOML files, provider keys, and
FreeLLMPool proxy; Hunter communicates through `PoolControlClient` only.
