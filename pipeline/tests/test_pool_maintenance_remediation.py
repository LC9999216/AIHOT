"""Offline maintenance exclusion: real child lock, fixture keys only."""
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading

import pytest


@pytest.mark.parametrize("command", ["register-provider", "enter-key", "provider-status", "remove-provider"])
def test_service_lock_blocks_all_local_maintenance(outside_repo_tmp_path, command):
    staging = outside_repo_tmp_path / "staging"
    production = outside_repo_tmp_path / "production"
    production.mkdir()
    holder_script = '''
import sys
from pathlib import Path
from hunter.runtime.locks import ProcessFileLock
lock = ProcessFileLock(Path(sys.argv[1]))
lock.acquire()
print("READY", flush=True)
try: sys.stdin.readline()
finally: lock.release()
'''
    caller_script = '''
import sys
import hunter.pool_service as service
import getpass
calls = []
def forbidden(*args, **kwargs):
    calls.append("called")
    raise AssertionError("local read or secret prompt before lock")
service._build_pool_components = forbidden
getpass.getpass = forbidden
code = service.main(sys.argv[1:])
assert not calls, "component construction or getpass reached"
sys.exit(code)
'''
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    holder = subprocess.Popen([sys.executable, "-B", "-c", holder_script, str(production / ".pool-control-service.lock")], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    ready = queue.Queue()
    reader = threading.Thread(target=lambda: ready.put(holder.stdout.readline()), daemon=True)
    reader.start()
    try:
        assert ready.get(timeout=10).strip() == "READY"
        args = [command, "--staging-dir", str(staging), "--production-dir", str(production), "--provider-id", "acme"]
        if command == "register-provider":
            args += ["--base-url", "https://example.invalid/v1"]
        if command == "remove-provider":
            args += ["--confirm", "acme"]
        result = subprocess.run([sys.executable, "-B", "-c", caller_script, *args], capture_output=True, text=True, env=env, timeout=15)
        assert result.returncode == 1
        assert result.stdout.strip() == "pool_control_service_running"
        assert not result.stderr
    finally:
        if holder.poll() is None:
            holder.communicate("release\n", timeout=10)
        reader.join(timeout=2)


@pytest.mark.parametrize("action", ["remove", "reregister"])
@pytest.mark.parametrize("shared", [False, True])
def test_key_cleanup_requires_no_remaining_reference(outside_repo_tmp_path, action, shared):
    from hunter.pool_control import PoolControl
    from hunter.pool_toml import read_config_keys

    pc = PoolControl(outside_repo_tmp_path / "staging", outside_repo_tmp_path / "production",
                     read_secret=lambda prompt: "offline-fixture-key")
    pc.register_provider("acme", base_url="https://example.invalid/v1", key_env="SHARED_KEY")
    assert pc.enter_key("acme")["configured"]
    if shared:
        pc.register_provider("beacon", base_url="https://example.invalid/v1", key_env="SHARED_KEY")
    if action == "remove":
        result = pc.remove_provider("acme")
        assert result["key_removed"] is (not shared)
        if shared:
            assert result["key_retained_reason"] == "referenced_by_remaining_provider"
    else:
        pc.register_provider("acme", base_url="https://example.invalid/v1", key_env="NEW_KEY")
    keys = read_config_keys(outside_repo_tmp_path / "staging" / "config.toml")
    assert ("SHARED_KEY" in keys) is shared
    if shared:
        assert pc.status("beacon").key_configured
