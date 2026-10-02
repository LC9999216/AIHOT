# Stage 2 Hardening Closeout

## Baseline

- Starting branch/HEAD: `codex/stage2-hardening-fixes` at
  `ef1d7691dc58978d566f0d00bdb74888e3a50c49`.
- Initial full offline suite: `3 failed, 589 passed, 1 skipped`; the failures
  were loopback Pool API requests routed through the environment proxy.
- Initial `git diff --check 02ce0f9..HEAD` reported trailing whitespace at
  `tests/test_runtime_orchestrator.py:51`.
- Protected data SHA-256 at start:
  - `data/providers.json`: `B885DA24472E10286974AA98030ADB28EA5C66DCF61947B1040B2AECA9A41EBB`
  - `data/candidates.json`: `AA40A455096FE39B5E7195CD101BAAA45160B8248F9DF0932B62DFBB9819B61D`
  - `data/evidence.json`: `8B04685F9AA082FB693DC2C04BB566AD81A117BE365D4E4E4D2F2C10F19A6A05`
  - `data/history.jsonl`: `910D1811363F26A29225EFB27AF0DEFF32C6BBF717E9D0EEDD494D2CA71A0727`

## Completed fixes

| Finding | Files | Offline regression | Commit |
|---|---|---|---|
| Test timing and external temporary-path isolation | `pool_api.py`, `tests/conftest.py`, Stage 2 tests | server readiness and isolated CLI test | `21b9f68` |
| Loopback, no redirect, no system proxy, constant-time bearer check | `pool_api.py`, `test_pool_control_fixes.py` | 46 passed, 1 POSIX-only skipped | `55ae414` |
| Live proxy ownership and persisted containment | `pool_proxy.py`, `pool_control.py`, `pool_toml.py` | 113 passed, 1 skipped | `0b4d84e` |
| Dedicated service and Hunter client-only boundary | `pool_service.py`, API/CLI/runner files | 118 passed, 1 skipped | `939fcf9` |
| Exact optional FreeLLMPool dependency | `pyproject.toml`, README, service | package contract and service version check | `1134c28` |
| Transactional failure paths | `runtime/orchestrator.py`, proxy/runtime tests | 182 passed, 1 skipped | `5f37d44` |

## Verification executed

- Targeted Task 2 readiness and external-temp tests passed twice: `2 passed`.
- Loopback boundary suite: `46 passed, 1 skipped`.
- Live proxy/containment suite: `113 passed, 1 skipped`.
- Service-boundary suite: `118 passed, 1 skipped`.
- Stage 2 security suite: `182 passed, 1 skipped`.
- Dependency metadata assertion passed; installed `freellmpool` assertion was
  locally verified as `0.13.0`.

## Final offline acceptance

- Complete suite, serial run 1: `626 passed, 1 skipped in 245.13s`.
- Complete suite, serial run 2: `626 passed, 1 skipped in 219.79s`.
  The sole skip is the POSIX-only permission-bit assertion on Windows.
- `python -B -m compileall -q src`: passed.
- `git diff --check 02ce0f9..HEAD`: passed with no output.
- `python -m hunter --help`, `python -m hunter pool --help`,
  `python -m hunter runtime --help`, and `hunter-pool-control --help`: passed.
- `freellmpool.__version__ == "0.13.0"`: passed.
- Protected data SHA-256 remained unchanged:
  - `data/providers.json`: `B885DA24472E10286974AA98030ADB28EA5C66DCF61947B1040B2AECA9A41EBB`
  - `data/candidates.json`: `AA40A455096FE39B5E7195CD101BAAA45160B8248F9DF0932B62DFBB9819B61D`
  - `data/evidence.json`: `8B04685F9AA082FB693DC2C04BB566AD81A117BE365D4E4E4D2F2C10F19A6A05`
  - `data/history.jsonl`: `910D1811363F26A29225EFB27AF0DEFF32C6BBF717E9D0EEDD494D2CA71A0727`
- Ruff, mypy, and bandit are not configured in `pyproject.toml`; none was run.

## Live verification

- FreeLLMPool real provider canary: not run without credential and authorization.
- Feishu: not run without webhook and authorization.
- Codex/OpenCode/Agent clients: not run without approved production proxy.

## Remaining risks

- Offline fakes prove control flow and fail-closed handling, not a real
  provider credential, external proxy deployment, or client interoperability.
- The service must be started in its own environment with directories outside
  the Hunter repository and named control variables configured by the operator.
