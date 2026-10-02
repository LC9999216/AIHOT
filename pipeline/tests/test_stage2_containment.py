"""Offline real process contention tests: fixture Pool Control, no upstream calls."""
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading

import pytest

from hunter.pool_api import PoolControlClient, PoolControlServer
from hunter.registry.schema import ProviderStatus
from hunter.runtime.outbox import OutboxStore
from hunter.runtime.stage2 import Stage2Runner
from tests.test_stage2_runner import _seed, _drive_to_approval, _registry_provider


@pytest.mark.parametrize("suspend_fails", [False, True])
def test_external_drain_lock_does_not_gate_containment(outside_repo_tmp_path, suspend_fails):
    data_dir, registry, evidence, pool = _seed(outside_repo_tmp_path / "case", hunter_root=None)
    _drive_to_approval(data_dir, pool)
    pool.promote("acme")
    registry.upsert_provider(_registry_provider(status=ProviderStatus.NOT_FREE), reason="withdrawn", source_metadata={"fixture": True})
    if suspend_fails:
        pool.suspend = lambda pid: {"suspended": False, "error": "fixture_failure"}

    script = '''
import sys
from pathlib import Path
from hunter.runtime.outbox import OutboxStore
with_path = Path(sys.argv[1])
store = OutboxStore(with_path)
try:
    print("LOCKED_READY", flush=True)
    assert sys.stdin.readline().strip() == "release"
finally:
    store.close()
'''
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    child = subprocess.Popen([sys.executable, "-B", "-c", script, str(data_dir / "notification_outbox.json")], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    ready = queue.Queue()
    reader = threading.Thread(target=lambda: ready.put(child.stdout.readline()), daemon=True)
    reader.start()
    server = PoolControlServer(pool, "offline-control-fixture")
    server.start_background()
    try:
        assert ready.get(timeout=10).strip() == "LOCKED_READY"
        runner = Stage2Runner(data_dir=data_dir, registry=registry, evidence_store=evidence, pool_control=PoolControlClient(server.url, "offline-control-fixture"))
        summary = runner.run()
        if suspend_fails:
            assert summary.suspend_failed == ["acme"]
            assert pool.production_halted
        else:
            assert summary.suspended == ["acme"]
            assert pool.list_production() == []
            assert not pool.production_halted
        intent_path = data_dir / "suspension_alert_intents.json"
        assert "acme" in json.loads(intent_path.read_text(encoding="utf-8"))
        _, errors = child.communicate("release\n", timeout=10)
        assert child.returncode == 0, errors
        # Replay pending intent after release without another upstream call.
        from hunter.runtime.stage2 import Stage2Summary
        runner._recover_suspension_alerts(Stage2Summary())
        assert json.loads(intent_path.read_text(encoding="utf-8")) == {}
        outbox = OutboxStore(data_dir / "notification_outbox.json")
        try:
            messages = outbox.pending()
            assert len(messages) == 1
            assert messages[0].event_type == "PRODUCTION_BLOCKED"
            assert "requested" in messages[0].title
        finally:
            outbox.close()
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=10)
        server.shutdown()
        reader.join(timeout=2)


@pytest.mark.parametrize("mode", ["read", "write"])
def test_intent_journal_failure_does_not_skip_remaining_providers(outside_repo_tmp_path, monkeypatch, mode):
    """Two invalid production providers: a malformed/unwritable alert-intent
    journal must not stop containment of the second provider. The storage
    failure is reported only AFTER every required containment action."""
    data_dir, registry, evidence, pool = _seed(
        outside_repo_tmp_path / "case",
        providers=[_registry_provider("acme"), _registry_provider("beacon")],
        hunter_root=None,
    )
    for pid in ("acme", "beacon"):
        _drive_to_approval(data_dir, pool, pid)
        pool.promote(pid)
        registry.upsert_provider(
            _registry_provider(pid, status=ProviderStatus.NOT_FREE), reason="withdrawn", source_metadata={"fixture": True}
        )
    journal = data_dir / "suspension_alert_intents.json"
    if mode == "read":
        journal.write_text("{not valid json", encoding="utf-8")
    else:
        class BrokenWriter:
            def __call__(self, path, payload):
                raise OSError("fixture: intent journal write denied")
        monkeypatch.setattr("hunter.runtime.stage2.Stage2Runner._save_suspension_intents", lambda self, intents: BrokenWriter()(journal, intents))

    from dataclasses import asdict
    real_status = pool.status

    def dict_status(provider_id):
        return asdict(real_status(provider_id))

    pool.status = dict_status  # type: ignore[method-assign]

    runner = Stage2Runner(data_dir=data_dir, registry=registry, evidence_store=evidence, pool_control=pool)
    from hunter.runtime.stage2 import Stage2Error
    with pytest.raises(Stage2Error, match="suspension_alert_intent_write_failed"):
        runner.run()
    # Failure must surface, but only AFTER containing BOTH providers.
    assert pool.list_production() == []
    if mode == "read":
        # A malformed journal is NOT silently overwritten by this round.
        assert journal.read_text(encoding="utf-8") == "{not valid json"
