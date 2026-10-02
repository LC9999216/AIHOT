"""Offline cross-process drain exclusion; no real webhook or credentials."""
import json
import os
from pathlib import Path
import subprocess
import sys

from hunter.runtime.outbox import OutboxMessage, OutboxStore


def test_concurrent_drain_is_excluded_until_delivery_finishes(tmp_path):
    path = tmp_path / "notification_outbox.json"
    store = OutboxStore(path)
    store.enqueue(OutboxMessage("evt-concurrent", "acme", "Acme", "POOL_SUSPENDED", "Suspend", "Public notice"))
    store.close()
    script = r'''
import sys
from pathlib import Path
from hunter.cli import main
import hunter.runtime.notify as notify
from hunter.runtime.locks import LockHeldError
class Adapter:
    def __init__(self, url): pass
    def send(self, message):
        print("SEND_READY", flush=True)
        assert sys.stdin.readline().strip() == "release"
notify.WebhookFeishuAdapter = Adapter
try:
    result = main(["notifications", "drain", "--data-dir", sys.argv[1]])
except LockHeldError:
    print("LOCKED", flush=True)
    result = 3
sys.exit(result)
'''
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    env["HUNTER_FEISHU_WEBHOOK_URL"] = "https://example.invalid/offline"
    command = [sys.executable, "-B", "-c", script, str(tmp_path)]
    first = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    try:
        # Explicit pipe handshake with timeout; never unbounded blocking.
        import queue as _queue
        import threading as _threading
        ready: _queue.Queue = _queue.Queue()
        reader = _threading.Thread(target=lambda: ready.put(first.stdout.readline()), daemon=True)
        reader.start()
        assert ready.get(timeout=15).strip() == "SEND_READY"
        second = subprocess.run(command, input="release\n", capture_output=True, text=True, env=env, timeout=15)
        assert second.returncode == 3, second.stderr
        assert "LOCKED" in second.stdout
        assert "SEND_READY" not in second.stdout
        output, errors = first.communicate("release\n", timeout=15)
        assert first.returncode == 0, errors
        assert json.loads(output)["sent"] == ["evt-concurrent"]
        third = subprocess.run(command, input="release\n", capture_output=True, text=True, env=env, timeout=15)
        assert third.returncode == 0, third.stderr
        assert "SEND_READY" not in third.stdout
        assert json.loads(third.stdout)["sent"] == []
    finally:
        if first.poll() is None:
            first.kill()
            first.communicate(timeout=15)
