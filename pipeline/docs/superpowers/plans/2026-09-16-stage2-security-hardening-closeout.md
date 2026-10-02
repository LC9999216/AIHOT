# Stage 2 Security Hardening Closeout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the remaining Stage 2 release blockers by enforcing the loopback/process trust boundary, making production containment affect the running FreeLLMPool proxy, stabilizing the offline acceptance suite, and making the Stage 2 dependency and deployment path reproducible.

**Architecture:** A dedicated Pool Control process owns the staging/production TOML files, raw provider keys, and the in-process FreeLLMPool production proxy. Hunter talks only to an authenticated `127.0.0.1` API through `PoolControlClient`; promotion and suspension reload the running proxy and verify its loaded provider set, while a persisted halt latch prevents restart or promotion after containment failure. The existing Stage 1 registry, evidence, scoring, and lifecycle contracts remain unchanged.

**Tech Stack:** Python 3.12, standard-library `http.server`/`urllib`/`threading`, FreeLLMPool 0.13.0 public `Pool` and `proxy.serve` APIs, Pydantic, pytest, TOML/JSON atomic persistence.

---

## 1. Execution boundary and success criteria

### Immutable starting point

- Repository: `D:\AI\Free token\Free Token Hunter`
- Read-only source branch: `codex/stage2-hardening-fixes`
- Required starting HEAD: `ef1d7691dc58978d566f0d00bdb74888e3a50c49`
- New implementation branch: `codex/stage2-hardening-final-fixes`
- New worktree: `D:\AI\Free token\Free Token Hunter-stage2-final-fixes`
- Existing commits `02ce0f9..ef1d769` are immutable. Do not rebase, amend, squash, or rewrite them.

### Protected scope

- Do not modify `main`, push, open a PR, merge, deploy, or start a live production proxy without a separate user request.
- Do not modify the Stage 1 public models, enum values, scoring weights, confirmation gates, SSRF limits, or trust-anchor rules in `AGENTS.md`.
- Do not modify existing `data/*.json` or place credentials, cookies, tokens, headers, environment dumps, or full upstream responses in Hunter files, tests, logs, history, outbox, or Git.
- Do not add Feishu features, account registration, key generation, CAPTCHA automation, multi-account rotation, routing policy, dashboards, databases, queues, or distributed workers.
- Do not use `git reset --hard`, `git checkout .`, `git restore .`, broad cleanup, or `git add .`.

### Definition of done

The branch is acceptable only when all of the following are true:

1. Pool Control server and client reject every non-`127.0.0.1` endpoint and every redirect before a Bearer token can leave the intended origin.
2. `hunter run-stage-two` constructs `PoolControlClient`; it never reads Pool TOML files or provider keys.
3. The dedicated Pool Control service owns FreeLLMPool 0.13.0 proxy startup, reload, shutdown, and loaded-provider readback.
4. Promotion/suspension success means the running proxy, not only TOML, reflects the new provider set.
5. A containment halt is persisted before shutdown, survives restart, blocks promotion, and cannot be cleared through the HTTP API.
6. The HTTP tests are deterministic in the full suite, and all tests use pytest/system temporary directories rather than a hard-coded sibling such as `D:\AI\dsh-tmp-test`.
7. FreeLLMPool 0.13.0 is declared as an optional Stage 2 dependency and checked at runtime.
8. The full offline suite passes twice consecutively; compile, whitespace, CLI, and clean-worktree gates pass.
9. Live provider, Feishu, Codex, OpenCode, and real-client E2E remain explicitly marked unverified unless credentials and deployment authorization are separately provided.

## 2. File responsibility map

### Create

- `src/hunter/pool_proxy.py` — owns the live FreeLLMPool proxy instance, reload/halt operations, and loaded-provider readback.
- `src/hunter/pool_service.py` — executable boundary that owns `PoolControl`, `FreellmpoolProxySupervisor`, and the loopback control server.
- `tests/test_pool_proxy.py` — offline tests for proxy start/reload/halt and fail-closed behavior.
- `tests/test_pool_service.py` — service wiring and secret-boundary tests.
- `docs/progress/STAGE_2_HARDENING_CLOSEOUT.md` — factual completion record with offline/live separation.

### Modify

- `src/hunter/pool_api.py` — strict loopback URL validation, redirect refusal, constant-time Bearer comparison, `/pool/status`, and removal of remote resume.
- `src/hunter/pool_control.py` — persisted halt latch, proxy-supervisor integration, transactional config/reload/readback, and rollback.
- `src/hunter/cli_imports.py` — Stage 2 runner uses only `PoolControlClient` plus named environment variables.
- `src/hunter/runtime/stage2.py` — depend on a narrow Pool Control protocol and use sanitized pool status readback.
- `pyproject.toml` — Stage 2 optional dependency and Pool Control service entry point.
- `tests/conftest.py` — reusable outside-repository temporary directory fixture.
- `tests/test_pool_control_fixes.py` — loopback/redirect/halt/readiness regressions.
- `tests/test_stage2_runner.py` — no hard-coded `D:\AI` path; assert client-only CLI wiring.
- `tests/test_runtime_orchestrator.py` — remove the existing trailing whitespace only; do not weaken assertions.
- `README.md` — exact offline install and two-process startup commands, without secrets.
- `docs/plans/2026-09-12-stage2-safe-pool.md` — record the now-explicit `/pool/status` readback endpoint and proxy ownership semantics.

### Do not modify unless a failing regression proves it necessary

- `src/hunter/evidence/**`
- `src/hunter/registry/**`
- `src/hunter/scoring/**`
- `data/**`
- `config/sources.yaml`
- `AGENTS.md`

## 3. Architectural decision record

Three approaches were considered:

1. **Recommended: Pool Control owns the in-process FreeLLMPool server.** It can atomically coordinate TOML, proxy reload, halt, and live readback without exposing keys to Hunter. A brief reload outage is acceptable and safer than routing stale providers.
2. **Rejected: keep the proxy externally launched and only edit TOML.** FreeLLMPool 0.13.0 builds a `Pool` at startup; TOML readback does not prove the running proxy reloaded, so suspension cannot guarantee traffic stopped.
3. **Rejected: locate and kill an arbitrary OS process.** PID discovery and command matching are platform-specific, racy, and risk terminating an unrelated process. The service must own the server object it controls.

The implementation therefore uses the public `from freellmpool import Pool` and `from freellmpool.proxy import serve` surface verified in local FreeLLMPool 0.13.0. No private FreeLLMPool modules or undocumented enable/disable methods are introduced.

---

### Task 1: Create the isolated worktree and capture the failing baseline

**Files:**
- Read: `AGENTS.md`
- Read: `docs/superpowers/plans/2026-09-16-stage2-security-hardening-closeout.md`
- Do not modify source in this task.

- [ ] **Step 1: Verify the immutable source checkout**

Run from `D:\AI\Free token\Free Token Hunter`:

```powershell
git status --short
git branch --show-current
git rev-parse HEAD
git log --oneline -7
```

Expected: clean status, branch `codex/stage2-hardening-fixes`, HEAD `ef1d7691dc58978d566f0d00bdb74888e3a50c49`, with the six repair commits above `02ce0f9`.

- [ ] **Step 2: Create the dedicated worktree**

```powershell
git worktree add 'D:\AI\Free token\Free Token Hunter-stage2-final-fixes' -b codex/stage2-hardening-final-fixes ef1d7691dc58978d566f0d00bdb74888e3a50c49
```

Expected: a new worktree on `codex/stage2-hardening-final-fixes`; the source checkout stays unchanged.

- [ ] **Step 3: Reproduce the known RED gates serially**

Run from the new worktree:

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
python -m pytest -p no:cacheprovider -q
git diff --check 02ce0f9..HEAD
```

Expected baseline from the 2026-09-16 audit: the full suite can report three `pool_control_unreachable` failures in `tests/test_pool_control_fixes.py`, and `git diff --check` reports trailing whitespace at `tests/test_runtime_orchestrator.py:51`. Record the actual result if the timing failures do not reproduce; do not fabricate failure output.

- [ ] **Step 4: Confirm protected data hashes**

```powershell
Get-FileHash data\providers.json,data\candidates.json,data\evidence.json,data\history.jsonl -Algorithm SHA256
```

Expected: four hashes recorded in the progress document later; no file is changed.

---

### Task 2: Make the offline acceptance harness deterministic

**Files:**
- Modify: `tests/conftest.py`
- Modify: `tests/test_pool_control_fixes.py:421-526`
- Modify: `tests/test_stage2_runner.py:380-408`
- Modify: `tests/test_runtime_orchestrator.py:51`

- [ ] **Step 1: Write failing tests for a reusable external temporary directory and server readiness**

Add to `tests/test_pool_control_fixes.py`:

```python
def test_server_start_background_waits_until_ready(tmp_path: Path) -> None:
    control = _control(tmp_path)
    server = PoolControlServer(control, bearer_token="test-bearer-token")
    try:
        thread = server.start_background(timeout=2.0)
        assert thread.is_alive()
        client = PoolControlClient(server.url, "test-bearer-token")
        assert client.status("missing")["provider_id"] == "missing"
    finally:
        server.shutdown()
```

Change `test_cli_run_stage_two_offline` to accept an `outside_repo_tmp_path: Path` fixture and use it directly:

```python
def test_cli_run_stage_two_offline(
    tmp_path: Path,
    outside_repo_tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    data_dir, registry, evidence_store, pool = _seed(tmp_path)
    pool_dir = outside_repo_tmp_path / "pool"
    pool_dir.mkdir()
```

- [ ] **Step 2: Run the new tests to verify RED**

```powershell
python -m pytest -p no:cacheprovider -q tests/test_pool_control_fixes.py::test_server_start_background_waits_until_ready tests/test_stage2_runner.py::test_cli_run_stage_two_offline
```

Expected: FAIL because `PoolControlServer.start_background` and `outside_repo_tmp_path` do not exist.

- [ ] **Step 3: Implement the temporary-directory fixture**

Add to `tests/conftest.py`:

```python
@pytest.fixture()
def outside_repo_tmp_path():
    path = Path(tempfile.mkdtemp(prefix="hunter-pool-test-"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)
```

Add `import tempfile` at the top. Remove lines 382-383 that construct `outside_repo` under the hard-coded `dsh-tmp-test` sibling directory, and remove that directory's manual cleanup from `test_cli_run_stage_two_offline`.

- [ ] **Step 4: Implement bounded server readiness**

Add state to `PoolControlServer.__init__` and a complete start method in `src/hunter/pool_api.py`:

```python
self._thread: Optional[threading.Thread] = None
```

```python
def start_background(self, timeout: float = 2.0) -> threading.Thread:
    if self._thread is not None:
        raise PoolApiError("server_already_started")
    thread = threading.Thread(
        target=self._httpd.serve_forever,
        name="hunter-pool-control",
        daemon=True,
    )
    self._thread = thread
    thread.start()
    deadline = time.monotonic() + max(0.01, timeout)
    while time.monotonic() < deadline:
        connection = http.client.HTTPConnection(self.host, self.port, timeout=0.1)
        try:
            connection.request("GET", "/")
            if connection.getresponse().status == 401:
                return thread
        except OSError:
            pass
        finally:
            connection.close()
        thread.join(timeout=0.01)
    raise PoolApiError("server_start_timeout")
```

Update the test `_serve()` helper to call `server.start_background()` instead of starting its own unsynchronized thread. Use the client's dedicated no-proxy opener introduced in Task 3; do not add sleeps.

- [ ] **Step 5: Remove the known whitespace error**

Delete the single trailing space after the separator comment at `tests/test_runtime_orchestrator.py:51`. Do not reformat any other line.

- [ ] **Step 6: Verify GREEN and repeat for timing stability**

```powershell
python -m pytest -p no:cacheprovider -q tests/test_pool_control_fixes.py tests/test_stage2_runner.py tests/test_runtime_orchestrator.py
python -m pytest -p no:cacheprovider -q tests/test_pool_control_fixes.py tests/test_stage2_runner.py tests/test_runtime_orchestrator.py
git diff --check
```

Expected: both test runs pass with the existing single POSIX-only skip; `git diff --check` prints nothing.

- [ ] **Step 7: Commit only this harness repair**

```powershell
git add tests/conftest.py tests/test_pool_control_fixes.py tests/test_stage2_runner.py tests/test_runtime_orchestrator.py src/hunter/pool_api.py
git diff --cached --check
git commit -m "test(stage2): stabilize pool API and temp paths"
```

---

### Task 3: Enforce the loopback API and prevent control-token redirects

**Files:**
- Modify: `src/hunter/pool_api.py`
- Modify: `tests/test_pool_control_fixes.py`

- [ ] **Step 1: Write RED tests for server, client, redirect, and token comparison boundaries**

Add these tests:

```python
class RedirectFixture:
    def __init__(self, location: str):
        self.location = location
        self.seen_authorization: list[str] = []
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                fixture.seen_authorization.append(
                    self.headers.get("Authorization", "")
                )
                self.send_response(302)
                self.send_header("Location", fixture.location)
                self.end_headers()

            def log_message(self, format: str, *args: object) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread: Optional[threading.Thread] = None

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> None:
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "localhost", "127.0.0.2"])
def test_http_server_rejects_noncanonical_loopback(tmp_path: Path, host: str) -> None:
    control = _control(tmp_path)
    with pytest.raises(PoolApiError, match="loopback_host_required"):
        PoolControlServer(control, bearer_token="token", host=host)


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:9000",
        "http://localhost:9000",
        "http://127.0.0.2:9000",
        "http://user@127.0.0.1:9000",
        "http://127.0.0.1:9000/path",
        "http://example.com:9000",
    ],
)
def test_pool_client_rejects_noncanonical_base_url(url: str) -> None:
    with pytest.raises(PoolApiError, match="loopback_base_url_required"):
        PoolControlClient(url, "token")


def test_pool_client_refuses_redirect_before_forwarding_token(tmp_path: Path) -> None:
    redirect = RedirectFixture(location="http://example.com/steal")
    redirect.start()
    try:
        client = PoolControlClient(redirect.base_url, "control-secret")
        with pytest.raises(PoolApiError, match="redirect_refused"):
            client.status("acme")
        assert redirect.seen_authorization == ["Bearer control-secret"]
    finally:
        redirect.stop()
```

The fixture binds only to `127.0.0.1`; the assertion proves the token reached only the configured origin before the redirect was refused. The test never starts a non-loopback listener or contacts `example.com`.

- [ ] **Step 2: Run RED tests**

```powershell
python -m pytest -p no:cacheprovider -q tests/test_pool_control_fixes.py -k "noncanonical or redirect"
```

Expected: FAIL because arbitrary hosts/base URLs are accepted and the default opener follows redirects.

- [ ] **Step 3: Add strict URL validation and a no-redirect opener**

Implement these helpers in `src/hunter/pool_api.py`:

```python
def _validate_loopback_base_url(base_url: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(base_url)
        port = parsed.port
    except ValueError as exc:
        raise PoolApiError("loopback_base_url_required") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or port is None
        or not 1 <= port <= 65535
    ):
        raise PoolApiError("loopback_base_url_required")
    return f"http://127.0.0.1:{port}"


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise PoolApiError("redirect_refused")
```

In `PoolControlServer.__init__`, require `host == "127.0.0.1"` before constructing `ThreadingHTTPServer`. In `PoolControlClient.__init__`, assign the validated URL and build `self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirectHandler())`. Change `_request()` to call `self._opener.open(request, timeout=self.timeout)` so environment proxies and redirects cannot receive the control token.

- [ ] **Step 4: Use constant-time Bearer comparison**

Change `_authorized()` to:

```python
return secrets.compare_digest(supplied, self.bearer_token)
```

Import `secrets`; retain the existing empty-token checks.

- [ ] **Step 5: Verify loopback security GREEN**

```powershell
python -m pytest -p no:cacheprovider -q tests/test_pool_control_fixes.py
```

Expected: all Windows-applicable tests pass; the only skip remains the POSIX permission-bit test.

- [ ] **Step 6: Commit the loopback boundary**

```powershell
git add src/hunter/pool_api.py tests/test_pool_control_fixes.py
git diff --cached --check
git commit -m "fix(pool): enforce loopback control boundary"
```

---

### Task 4: Add a live FreeLLMPool proxy supervisor and persisted halt latch

**Files:**
- Create: `src/hunter/pool_proxy.py`
- Create: `tests/test_pool_proxy.py`
- Modify: `src/hunter/pool_control.py`
- Modify: `src/hunter/pool_toml.py`
- Modify: `tests/test_pool_control_fixes.py`

- [ ] **Step 1: Write RED tests for persisted containment and live readback**

Create `tests/test_pool_proxy.py` with a fake server and fake Pool factory:

```python
class FakeProxySupervisor:
    def __init__(self, provider_ids: list[str], *, fail_reload: bool = False):
        self._provider_ids = set(provider_ids)
        self.fail_reload = fail_reload
        self.reload_calls = 0
        self.halt_calls = 0

    def reload(self) -> None:
        self.reload_calls += 1
        if self.fail_reload:
            raise RuntimeError("synthetic reload failure without secret material")

    def halt(self) -> None:
        self.halt_calls += 1
        self._provider_ids.clear()

    def running_provider_ids(self) -> set[str]:
        return set(self._provider_ids)

    def force_loaded(self, provider_ids: list[str]) -> None:
        self._provider_ids = set(provider_ids)


class FakeServer:
    def __init__(self, providers: list[str]):
        self.pool = SimpleNamespace(
            providers=[SimpleNamespace(id=provider_id) for provider_id in providers]
        )
        self.serve_calls = 0
        self.shutdown_calls = 0
        self.close_calls = 0

    def serve_forever(self) -> None:
        self.serve_calls += 1

    def shutdown(self) -> None:
        self.shutdown_calls += 1

    def server_close(self) -> None:
        self.close_calls += 1


def test_halt_survives_reconstruction(tmp_path: Path) -> None:
    supervisor = FakeProxySupervisor(["acme"])
    control = _control(tmp_path, proxy_supervisor=supervisor)
    assert control.stop_production()["halted"] is True
    rebuilt = _control(tmp_path, proxy_supervisor=FakeProxySupervisor(["acme"]))
    assert rebuilt.production_halted is True
    assert rebuilt.promote("acme")["error"] == "production_halted"


def test_promote_requires_live_proxy_readback(tmp_path: Path) -> None:
    supervisor = FakeProxySupervisor([])
    control = _control(tmp_path, proxy_supervisor=supervisor)
    _registered_with_key(control)
    result = control.promote("acme")
    assert result == {
        "provider_id": "acme",
        "promoted": False,
        "error": "live_proxy_readback_failed",
    }
    assert control.production_halted is True


def test_suspend_requires_provider_absent_from_live_proxy(tmp_path: Path) -> None:
    supervisor = FakeProxySupervisor(["acme"])
    control = _control(tmp_path, proxy_supervisor=supervisor)
    _registered_with_key(control)
    assert control.promote("acme")["promoted"] is True
    supervisor.force_loaded(["acme"])
    result = control.suspend("acme")
    assert result["suspended"] is False
    assert result["error"] == "live_proxy_readback_failed"
    assert control.production_halted is True
```

- [ ] **Step 2: Run RED tests**

```powershell
python -m pytest -p no:cacheprovider -q tests/test_pool_proxy.py tests/test_pool_control_fixes.py -k "halt or live_proxy"
```

Expected: FAIL because there is no proxy supervisor and halt state is process-local.

- [ ] **Step 3: Create the proxy-supervisor contract and implementation**

Create `src/hunter/pool_proxy.py` with this public surface:

```python
class ProxySupervisor(Protocol):
    def reload(self) -> None:
        raise NotImplementedError

    def halt(self) -> None:
        raise NotImplementedError

    def running_provider_ids(self) -> set[str]:
        raise NotImplementedError


class FreellmpoolProxySupervisor:
    def __init__(
        self,
        providers_path: Path,
        config_path: Path,
        *,
        host: str = "127.0.0.1",
        port: int = 8080,
        proxy_key: Optional[str] = None,
        pool_factory: Optional[Callable[[], Any]] = None,
        server_factory: Optional[Callable[[Any], Any]] = None,
    ):
        if host != "127.0.0.1":
            raise PoolControlError("proxy_loopback_host_required")
        self.providers_path = Path(providers_path)
        self.config_path = Path(config_path)
        self.host = host
        self.port = port
        self.proxy_key = proxy_key
        self._pool_factory = pool_factory or self._build_pool
        self._server_factory = server_factory or self._build_server
        self._server = None
        self._thread = None

    def _build_pool(self):
        from freellmpool import Pool
        expected_catalog = str(self.providers_path)
        expected_config = str(self.config_path)
        if os.environ.get("FREELLMPOOL_CONFIG") != expected_catalog:
            raise PoolControlError("freellmpool_catalog_path_mismatch")
        if os.environ.get("FREELLMPOOL_CONFIG_FILE") != expected_config:
            raise PoolControlError("freellmpool_config_path_mismatch")
        return Pool.from_default_config()

    def _build_server(self, pool):
        from freellmpool.proxy import serve
        return serve(pool, host=self.host, port=self.port, api_key=self.proxy_key)

    def reload(self) -> None:
        self.halt()
        server = self._server_factory(self._pool_factory())
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        self._server = server
        self._thread = thread
        thread.start()

    def halt(self) -> None:
        server = self._server
        self._server = None
        self._thread = None
        if server is not None:
            server.shutdown()
            server.server_close()

    def running_provider_ids(self) -> set[str]:
        server = self._server
        if server is None:
            return set()
        return {str(provider.id) for provider in server.pool.providers}
```

The verified 0.13.0 signature is `Pool.from_default_config(*, env=None, quota=None, post=default_post, on_event=None)`, but its internal `load_catalog()` call reads `FREELLMPOOL_CONFIG` from the process environment. Therefore `pool_service.py` must set `FREELLMPOOL_CONFIG` and `FREELLMPOOL_CONFIG_FILE` to the isolated production paths once, before building the supervisor. This mutation occurs only in the dedicated Pool Control process; no key value is copied into Hunter's environment or logs.

- [ ] **Step 4: Persist the halt latch atomically**

Add `atomic_write_json(path: Path, payload: dict[str, Any])` beside the existing atomic TOML helpers, using temp-file + flush + fsync + `os.replace`. In `PoolControl`, use `production_dir / "control_state.json"` with this exact schema:

```json
{"production_halted": true, "reason": "suspend_failed"}
```

On construction, strict-parse the file; malformed content raises `PoolControlError("invalid_control_state")`. `stop_production(reason)` writes `production_halted=true` before calling `proxy_supervisor.halt()`. There is no automatic clear on restart.

- [ ] **Step 5: Make promotion and suspension transactional against the live proxy**

For `promote(provider_id)`:

1. Reject when the persisted halt latch is set.
2. Snapshot production provider/key dictionaries in memory.
3. Write the new TOML atomically.
4. Call `proxy_supervisor.reload()`.
5. Require `provider_id in proxy_supervisor.running_provider_ids()`.
6. On reload/readback failure, restore the snapshot, persist halt, halt the proxy, and return `live_proxy_reload_failed` or `live_proxy_readback_failed` without reporting promotion.

For `suspend(provider_id)`:

1. Snapshot production provider/key dictionaries in memory.
2. Remove the provider and its key from TOML.
3. Call `proxy_supervisor.reload()`.
4. Require `provider_id not in proxy_supervisor.running_provider_ids()`.
5. On failure, persist halt and halt the proxy. Do not return `suspended=True` merely because TOML changed.

Do not serialize the in-memory snapshot or include key values in exceptions.

- [ ] **Step 6: Remove remote resume**

Delete `/pool/resume` handling from `PoolControlHandler` and `resume_production()` from `PoolControlClient`. Keep recovery as a service-startup operation added in Task 5, requiring an explicit console action after manual review.

- [ ] **Step 7: Verify proxy and containment GREEN**

```powershell
python -m pytest -p no:cacheprovider -q tests/test_pool_proxy.py tests/test_pool_control.py tests/test_pool_control_fixes.py tests/test_runtime_orchestrator.py
```

Expected: all Windows-applicable tests pass; failures never leak `SECRET` in captured output or JSON.

- [ ] **Step 8: Commit the live containment unit**

```powershell
git add src/hunter/pool_proxy.py src/hunter/pool_control.py src/hunter/pool_toml.py src/hunter/pool_api.py tests/test_pool_proxy.py tests/test_pool_control_fixes.py
git diff --cached --check
git commit -m "fix(pool): control live proxy and persist containment"
```

---

### Task 5: Make Pool Control a real separate process and Hunter client-only

**Files:**
- Create: `src/hunter/pool_service.py`
- Create: `tests/test_pool_service.py`
- Modify: `src/hunter/pool_api.py`
- Modify: `src/hunter/cli_imports.py`
- Modify: `src/hunter/runtime/stage2.py`
- Modify: `tests/test_stage2_runner.py`
- Modify: `pyproject.toml`

- [ ] **Step 1: Write RED tests for process-boundary wiring**

Add to `tests/test_pool_service.py`:

```python
class FakeServiceSupervisor:
    def __init__(self, provider_ids: list[str]):
        self._provider_ids = set(provider_ids)
        self.reload_calls = 0
        self.halt_calls = 0

    def reload(self) -> None:
        self.reload_calls += 1

    def halt(self) -> None:
        self.halt_calls += 1
        self._provider_ids.clear()

    def running_provider_ids(self) -> set[str]:
        return set(self._provider_ids)


def test_service_rejects_missing_control_token(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("HUNTER_POOL_CONTROL_TOKEN", raising=False)
    with pytest.raises(PoolServiceError, match="control_token_required"):
        build_service(
            staging_dir=tmp_path / "staging",
            production_dir=tmp_path / "production",
            host="127.0.0.1",
            port=8091,
            proxy_port=8080,
        )


def test_service_does_not_start_proxy_while_halted(tmp_path: Path) -> None:
    state = tmp_path / "production" / "control_state.json"
    state.parent.mkdir(parents=True)
    state.write_text('{"production_halted":true,"reason":"test"}', encoding="utf-8")
    supervisor = FakeServiceSupervisor([])
    service = build_service_for_test(tmp_path, supervisor)
    assert supervisor.reload_calls == 0
    assert service.pool_control.production_halted is True
```

Add to `tests/test_stage2_runner.py`:

```python
def test_run_stage_two_cli_uses_pool_client_only(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HUNTER_POOL_CONTROL_URL", "http://127.0.0.1:8091")
    monkeypatch.setenv("HUNTER_POOL_CONTROL_TOKEN", "control-token")
    captured: dict[str, object] = {}

    class FakeClient:
        def __init__(self, url: str, token: str):
            captured["url"] = url
            captured["token"] = token

    class FakeRunner:
        def __init__(self, *, data_dir: Path, pool_control: object):
            captured["data_dir"] = data_dir
            captured["pool_control"] = pool_control

        def run(self):
            return SimpleNamespace(
                to_dict=lambda: {"skipped_locked": False},
                skipped_locked=False,
                suspend_failed=[],
                pool_runtime_split=[],
            )

    monkeypatch.setattr("hunter.pool_api.PoolControlClient", FakeClient)
    monkeypatch.setattr("hunter.runtime.stage2.Stage2Runner", FakeRunner)
    assert cli_main(["run-stage-two", "--data-dir", str(tmp_path)]) == 0
    assert captured["url"] == "http://127.0.0.1:8091"
    assert isinstance(captured["pool_control"], FakeClient)
```

- [ ] **Step 2: Run RED tests**

```powershell
python -m pytest -p no:cacheprovider -q tests/test_pool_service.py tests/test_stage2_runner.py -k "service or client_only"
```

Expected: FAIL because the service module and client-only CLI wiring do not exist.

- [ ] **Step 3: Add sanitized global pool readback**

Add authenticated `GET /pool/status` returning only:

```json
{"production_ids": ["provider-a"], "production_halted": false}
```

Add to `PoolControlClient`:

```python
def pool_status(self) -> Dict[str, Any]:
    return self._request("GET", "/pool/status")


def list_production(self) -> List[str]:
    payload = self.pool_status()
    ids = payload.get("production_ids")
    if not isinstance(ids, list) or not all(isinstance(item, str) for item in ids):
        raise PoolApiError("invalid_pool_status")
    return sorted(set(ids))
```

This endpoint is required for `Stage2Runner._verify_pool_state()` to detect providers unknown to runtime; it exposes IDs only, never keys or upstream content.

- [ ] **Step 4: Create the dedicated service entry point**

Create `src/hunter/pool_service.py` with:

```python
@dataclass
class PoolService:
    pool_control: PoolControl
    proxy_supervisor: ProxySupervisor
    control_server: PoolControlServer

    def run(self) -> None:
        if not self.pool_control.production_halted:
            self.proxy_supervisor.reload()
        try:
            self.control_server.serve_forever()
        finally:
            self.proxy_supervisor.halt()
            self.control_server.close()


def build_service(
    *,
    staging_dir: Path,
    production_dir: Path,
    host: str,
    port: int,
    proxy_port: int,
) -> PoolService:
    token = os.environ.get("HUNTER_POOL_CONTROL_TOKEN", "")
    if not token:
        raise PoolServiceError("control_token_required")
    if host != "127.0.0.1":
        raise PoolServiceError("loopback_host_required")
    os.environ["FREELLMPOOL_CONFIG"] = str(production_dir / "providers.toml")
    os.environ["FREELLMPOOL_CONFIG_FILE"] = str(production_dir / "config.toml")
    supervisor = FreellmpoolProxySupervisor(
        production_dir / "providers.toml",
        production_dir / "config.toml",
        host="127.0.0.1",
        port=proxy_port,
        proxy_key=os.environ.get("FREELLMPOOL_PROXY_KEY") or None,
    )
    control = PoolControl(
        staging_dir,
        production_dir,
        probe_runner=FreellmpoolProbeRunner(),
        proxy_supervisor=supervisor,
    )
    server = PoolControlServer(control, token, host=host, port=port)
    return PoolService(
        pool_control=control,
        proxy_supervisor=supervisor,
        control_server=server,
    )
```

`PoolService.run()` starts the proxy only when the persisted halt latch is false, then serves the control API. Its `finally` block halts the proxy and closes the control server. Do not log token values or environment contents.

Add `PoolControlServer.close()` as a non-blocking wrapper around `self._httpd.server_close()`. Keep `shutdown()` for tests and callers that stop a server running on another thread; do not call `shutdown()` from the same thread that is executing `serve_forever()`.

Add an explicit recovery command `hunter-pool-control resume --confirm RESUME` that clears the persisted latch only after the literal confirmation matches. It runs inside the Pool Control process boundary and is not exposed over HTTP.

- [ ] **Step 5: Make Hunter Stage 2 client-only**

Replace the direct `PoolControl` construction in `_handle_run_stage_two` with:

```python
base_url = os.environ.get("HUNTER_POOL_CONTROL_URL", "")
token = os.environ.get("HUNTER_POOL_CONTROL_TOKEN", "")
if not base_url or not token:
    raise RuntimeError("pool control URL and token are required")
pool_control = PoolControlClient(base_url, token)
runner = Stage2Runner(data_dir=data_dir, pool_control=pool_control)
```

Remove `--staging-dir`, `--production-dir`, and `HUNTER_POOL_BASE_DIR` from the Hunter CLI path. Those belong only to the Pool Control service. Keep test fakes injected directly into `Stage2Runner`.

Define a narrow `PoolControlPort` `Protocol` in `runtime/stage2.py` containing only `status`, `probe`, `promote`, `suspend`, `stop_production`, and `list_production`, then type `Stage2Runner.pool_control` against it instead of concrete `PoolControl`.

- [ ] **Step 6: Add the service console script**

Add under `[project.scripts]`:

```toml
hunter-pool-control = "hunter.pool_service:main"
```

Do not add a command that accepts a provider key value through argv.

- [ ] **Step 7: Verify separation GREEN**

```powershell
python -m pytest -p no:cacheprovider -q tests/test_pool_service.py tests/test_stage2_runner.py tests/test_runtime_orchestrator.py tests/test_pool_control_fixes.py
python -m hunter run-stage-two --help
hunter-pool-control --help
```

Expected: tests pass; Hunter help has no staging/production directory flags; service help exposes directory/port configuration but no secret-value argument.

- [ ] **Step 8: Commit process isolation**

```powershell
git add src/hunter/pool_service.py src/hunter/pool_api.py src/hunter/cli_imports.py src/hunter/runtime/stage2.py tests/test_pool_service.py tests/test_stage2_runner.py pyproject.toml
git diff --cached --check
git commit -m "fix(stage2): isolate pool control service"
```

---

### Task 6: Declare and verify the exact Stage 2 dependency

**Files:**
- Modify: `pyproject.toml`
- Create or modify: `tests/test_dependency_contract.py`
- Modify: `README.md`

- [ ] **Step 1: Write a RED package-metadata test**

Create `tests/test_dependency_contract.py`:

```python
from pathlib import Path
import tomllib


def test_stage2_extra_pins_freellmpool_0130() -> None:
    config = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    stage2 = config["project"]["optional-dependencies"]["stage2"]
    assert "freellmpool==0.13.0" in stage2
```

- [ ] **Step 2: Run RED test**

```powershell
python -m pytest -p no:cacheprovider -q tests/test_dependency_contract.py
```

Expected: FAIL because the `stage2` extra does not exist.

- [ ] **Step 3: Add the exact optional dependency**

Add to `[project.optional-dependencies]`:

```toml
stage2 = [
    "freellmpool==0.13.0",
]
```

Keep it optional so Stage 1 installs do not acquire Stage 2 runtime dependencies. Retain `FreellmpoolProbeRunner.check_version()` and invoke the same check during Pool Control service startup before binding ports.

- [ ] **Step 4: Document reproducible installation without embedding secrets**

Add these commands to `README.md`:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev,stage2]"
.\.venv\Scripts\python.exe -c "import freellmpool; assert freellmpool.__version__ == '0.13.0'"
```

Document environment variable names only: `HUNTER_POOL_CONTROL_TOKEN`, `HUNTER_POOL_CONTROL_URL`, and `FREELLMPOOL_PROXY_KEY`. Do not show example values that resemble real credentials.

- [ ] **Step 5: Verify dependency contract GREEN**

```powershell
python -m pytest -p no:cacheprovider -q tests/test_dependency_contract.py
python -c "import freellmpool; assert freellmpool.__version__ == '0.13.0'"
```

Expected: test and import assertion pass.

- [ ] **Step 6: Commit dependency reproducibility**

```powershell
git add pyproject.toml tests/test_dependency_contract.py README.md
git diff --cached --check
git commit -m "build(stage2): pin freellmpool runtime"
```

---

### Task 7: Exercise transactional failure paths end to end offline

**Files:**
- Modify: `tests/test_pool_proxy.py`
- Modify: `tests/test_pool_control_fixes.py`
- Modify: `tests/test_runtime_orchestrator.py`
- Modify: `tests/test_stage2_runner.py`

- [ ] **Step 1: Add a RED matrix for every crash/failure boundary**

Add explicit tests for the missing live-proxy branches; retain the existing config-write and runtime-store failure tests.

```python
class RaisingProxySupervisor(FakeProxySupervisor):
    def reload(self) -> None:
        self.reload_calls += 1
        raise RuntimeError("synthetic reload failure without secret material")


def test_promotion_reload_failure_restores_config_and_persists_halt(
    tmp_path: Path,
) -> None:
    supervisor = RaisingProxySupervisor([])
    control = _control(tmp_path, proxy_supervisor=supervisor)
    _registered_with_key(control)
    result = control.promote("acme")
    assert result["promoted"] is False
    assert result["error"] == "live_proxy_reload_failed"
    assert control.list_production() == []
    assert supervisor.running_provider_ids() == set()
    rebuilt = _control(tmp_path, proxy_supervisor=FakeProxySupervisor([]))
    assert rebuilt.production_halted is True


def test_promotion_readback_failure_halts_and_removes_live_provider(
    tmp_path: Path,
) -> None:
    supervisor = FakeProxySupervisor([])
    control = _control(tmp_path, proxy_supervisor=supervisor)
    _registered_with_key(control)
    result = control.promote("acme")
    assert result["error"] == "live_proxy_readback_failed"
    assert control.list_production() == []
    assert control.production_halted is True


def test_suspension_reload_failure_never_reports_suspended(tmp_path: Path) -> None:
    supervisor = FakeProxySupervisor(["acme"])
    control = _control(tmp_path, proxy_supervisor=supervisor)
    _registered_with_key(control)
    assert control.promote("acme")["promoted"] is True
    supervisor.fail_reload = True
    result = control.suspend("acme")
    assert result["suspended"] is False
    assert result["error"] == "live_proxy_reload_failed"
    assert control.production_halted is True


def test_failure_artifacts_never_contain_secret(tmp_path: Path, caplog) -> None:
    supervisor = RaisingProxySupervisor([])
    control = _control(tmp_path, proxy_supervisor=supervisor)
    _registered_with_key(control)
    result = control.promote("acme")
    artifacts = json.dumps(result) + caplog.text
    artifacts += control.production_providers_path.read_text(encoding="utf-8")
    artifacts += (control.production_dir / "control_state.json").read_text(
        encoding="utf-8"
    )
    assert SECRET not in artifacts
```

Update `test_promote_state_split_halts_production_and_alerts` so it also asserts `result.error == "runtime_store_write_failed_split"`, the live supervisor has no loaded providers, and a reconstructed `PoolControl` remains halted. Keep `test_promote_rolls_back_pool_when_runtime_store_fails` expecting `runtime_store_write_failed_rolled_back` and an empty live provider set.

- [ ] **Step 2: Run the matrix to verify RED where behavior is missing**

```powershell
python -m pytest -p no:cacheprovider -q tests/test_pool_proxy.py tests/test_runtime_orchestrator.py -k "failure or split or rollback"
```

Expected: failures identify any branch that reports success, loses the halt latch, leaves a provider live, uses the wrong split error code, or leaks `SECRET`.

- [ ] **Step 3: Apply only the minimal failure-path corrections**

Use the result of each RED case to adjust the existing rollback branch. Do not refactor unrelated orchestration. Every exception returned across the API must use a stable error code and omit raw exception text if it could contain upstream content or secrets.

Required state table:

| Failure | Config outcome | Live proxy outcome | Runtime outcome |
|---|---|---|---|
| Config write before reload | old config retained | old live set retained | no promotion/suspension state |
| Proxy reload/readback after promotion | old config restored | halted | no `PRODUCTION` state |
| Proxy reload/readback after suspension | provider removed from config | halted | no false `SUSPENDED` state |
| Runtime write after live promotion | rollback suspension attempted | provider absent or proxy halted | no success |
| Rollback failure | best-effort config removal | proxy halted | split alert persisted |

- [ ] **Step 4: Run all Stage 2 security tests GREEN**

```powershell
python -m pytest -p no:cacheprovider -q tests/test_pool_proxy.py tests/test_pool_service.py tests/test_pool_control.py tests/test_pool_control_fixes.py tests/test_runtime_orchestrator.py tests/test_runtime_fixes.py tests/test_stage2_runner.py tests/test_outbox_fixes.py tests/test_stage5.py
```

Expected: all Windows-applicable tests pass; only explicitly platform-specific tests skip.

- [ ] **Step 5: Commit transactional closeout**

```powershell
git add tests/test_pool_proxy.py tests/test_pool_control_fixes.py tests/test_runtime_orchestrator.py tests/test_stage2_runner.py src/hunter/pool_control.py src/hunter/pool_proxy.py src/hunter/runtime/orchestrator.py
git diff --cached --check
git commit -m "test(stage2): close transactional failure paths"
```

---

### Task 8: Update authoritative Stage 2 documentation and progress evidence

**Files:**
- Modify: `docs/plans/2026-09-12-stage2-safe-pool.md`
- Create: `docs/progress/STAGE_2_HARDENING_CLOSEOUT.md`
- Modify: `README.md`

- [ ] **Step 1: Update the architecture document**

Record these exact facts:

- Pool Control owns and reloads the production FreeLLMPool proxy.
- Hunter has no staging/production path access and talks through `PoolControlClient` only.
- Control API binds exactly `127.0.0.1`, rejects redirects, and exposes sanitized `/pool/status` for reconciliation.
- The halt latch is persisted before proxy shutdown and can only be cleared through the explicit local service recovery command.
- Config-file readback is necessary but not sufficient; live loaded-provider readback is required.

- [ ] **Step 2: Create the progress document with evidence slots already resolved by commands**

Use this structure in `docs/progress/STAGE_2_HARDENING_CLOSEOUT.md`:

```markdown
# Stage 2 Hardening Closeout

## Baseline
- Starting branch and HEAD
- Protected data SHA-256 values

## Completed fixes
- Finding -> files -> regression tests -> commit

## Verification executed
- Exact command
- Exit code
- Passed/failed/skipped count

## Live verification
- FreeLLMPool real provider canary: not run without credential and authorization
- Feishu: not run without webhook and authorization
- Codex/OpenCode/Agent clients: not run without approved production proxy

## Remaining risks
- Only risks supported by current evidence
```

Do not write “all verified,” “production ready,” or “complete” when a live gate was not run.

- [ ] **Step 3: Document the two-process startup order**

README must show:

1. Install `.[dev,stage2]`.
2. Set named environment variables without printing values.
3. Start `hunter-pool-control serve` in the isolated Pool Control environment.
4. Run `python -m hunter run-stage-two` in the Hunter environment.
5. Interpret nonzero exit codes as fail-closed results.

- [ ] **Step 4: Verify documentation consistency**

```powershell
rg -n "PoolControl\(|HUNTER_POOL_BASE_DIR|--staging-dir|--production-dir|/pool/resume|592 passed" README.md docs src/hunter/cli_imports.py
```

Expected: no documentation tells Hunter to instantiate `PoolControl`, use Pool paths, call remote resume, or repeat the obsolete `592 passed` claim. Historical reports may retain dated evidence only if clearly labeled historical.

- [ ] **Step 5: Commit documentation and progress**

```powershell
git add docs/plans/2026-09-12-stage2-safe-pool.md docs/progress/STAGE_2_HARDENING_CLOSEOUT.md README.md
git diff --cached --check
git commit -m "docs(stage2): record hardened service boundary"
```

---

### Task 9: Run final offline acceptance and prepare the delivery report

**Files:**
- Verify all changed files.
- Update: `docs/progress/STAGE_2_HARDENING_CLOSEOUT.md` with actual results only.

- [ ] **Step 1: Run the full suite twice, serially**

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
python -m pytest -p no:cacheprovider -q
python -m pytest -p no:cacheprovider -q
```

Expected: both runs exit 0 with identical pass/skip counts. If either run fails, stop, diagnose with a new failing minimal test, fix, and rerun both; do not call the gate complete.

- [ ] **Step 2: Run compile, whitespace, and CLI gates**

```powershell
python -B -m compileall -q src
git diff --check 02ce0f9..HEAD
python -m hunter --help
python -m hunter pool --help
python -m hunter runtime --help
hunter-pool-control --help
```

Expected: every command exits 0; `git diff --check` prints nothing.

- [ ] **Step 3: Verify dependency and boundary facts**

```powershell
python -c "import freellmpool; assert freellmpool.__version__ == '0.13.0'"
rg -n "from \.pool_control import PoolControl|PoolControl\(" src/hunter/cli_imports.py src/hunter/runtime/stage2.py
rg -n "HUNTER_POOL_BASE_DIR|--staging-dir|--production-dir" src/hunter/cli_imports.py README.md
```

Expected: version assertion passes; Hunter runtime/CLI files do not instantiate concrete `PoolControl` or accept Pool directories.

- [ ] **Step 4: Verify no secret-like material or protected-data drift**

```powershell
git diff --name-only ef1d7691dc58978d566f0d00bdb74888e3a50c49..HEAD
Get-FileHash data\providers.json,data\candidates.json,data\evidence.json,data\history.jsonl -Algorithm SHA256
git status --short
```

Expected: no `data/*.json` changes; hashes match Task 1; status is clean after the final progress update commit.

- [ ] **Step 5: Record tool limitations accurately**

If `ruff`, `mypy`, and `bandit` remain absent from `pyproject.toml`, write “not configured; not run.” Do not install or configure them as part of this repair. If the repository already configures them by execution time, run only the existing configured commands and record exact results.

- [ ] **Step 6: Commit the final factual progress update**

```powershell
git add docs/progress/STAGE_2_HARDENING_CLOSEOUT.md
git diff --cached --check
git commit -m "docs(stage2): record final offline acceptance"
```

- [ ] **Step 7: Produce the final delivery summary**

The final report must include:

- branch and exact final HEAD;
- commit list above `ef1d769`;
- finding-to-file-to-test mapping;
- exact results for both full pytest runs, compileall, diff-check, CLI help, dependency version, hashes, and Git status;
- completed, partial, and unverified sections;
- explicit statement that no push, PR, merge, deployment, live credentials, or live provider requests occurred;
- remaining live E2E commands only after credentials and user authorization are provided.

## 4. Stop conditions

Stop and report instead of improvising if any of these occur:

1. Installed FreeLLMPool is not exactly 0.13.0 or its public signatures differ from the verified package.
2. A safe live-proxy reload cannot be implemented using public FreeLLMPool APIs without copying or modifying the dependency.
3. A test would require a real provider key, webhook, production proxy, external message, paid action, or network call.
4. A required fix would change Stage 1 contracts, scoring, trust policy, security limits, or protected data.
5. The worktree is dirty before implementation or protected data hashes change.
6. Pool halt/reload cannot prove the provider is absent from the running proxy.
7. Full tests remain timing/order-dependent after the deterministic harness changes.

## 5. Copyable Agent handoff prompt

```text
请在一个新的隔离 worktree 中直接执行 Stage 2 安全收尾计划，不要重新规划。

仓库：D:\AI\Free token\Free Token Hunter
计划文件：D:\AI\Free token\Free Token Hunter\docs\superpowers\plans\2026-09-16-stage2-security-hardening-closeout.md
只读起点：codex/stage2-hardening-fixes@ef1d7691dc58978d566f0d00bdb74888e3a50c49
新 worktree：D:\AI\Free token\Free Token Hunter-stage2-final-fixes
新分支：codex/stage2-hardening-final-fixes

核心目标：修复剩余的 Pool Control 回环边界、控制令牌重定向、真实 production proxy reload/readback、持久化 halt、Hunter/Pool Control 进程隔离、测试时序与硬编码临时目录，以及 freellmpool==0.13.0 依赖声明。Hunter 必须只使用 PoolControlClient，原始 Key 和 Pool TOML 只能存在于独立 Pool Control 进程边界内。

严格按计划 Task 1→9 串行执行。每个行为修改必须先写最小失败测试并确认 RED，再做最小实现并确认 GREEN；每个任务单独精确暂存和提交，禁止 git add .。保留 02ce0f9..ef1d769 的既有提交，不得 rebase/amend/squash，不得修改 main。

保护项：不得修改 AGENTS.md 的 Stage 1 公共契约、枚举、评分权重、确认门禁、信任锚或安全限制；不得修改 data/*.json；不得写入或打印真实 Key、token、cookie、header、环境变量值或完整上游响应；不得 push、PR、merge、deploy，不得进行 live provider/飞书/客户端调用。禁止 git reset --hard、git checkout .、git restore . 和广泛清理。

最终门禁：完整 pytest 串行连续通过两次；python -B -m compileall -q src；git diff --check 02ce0f9..HEAD；hunter、pool、runtime、hunter-pool-control help；freellmpool 版本断言；受保护 data 哈希不变；git status clean。ruff/mypy/bandit 若仍未配置，必须明确写未运行，不得临时扩 scope。

最终交付请给出：分支与最终 HEAD、提交链、问题→文件→回归测试映射、所有命令的真实结果、受保护数据哈希、完成/部分/未验证三类状态、剩余 live 风险，以及“未 push/PR/merge/deploy、未使用真实凭据”的明确声明。遇到计划中的 Stop conditions 时停止并报告，不要自行扩大范围。
```
