"""Root pytest configuration for Free Token Hunter.

The execution sandbox denies writes into directories created with
mode=0o700 (which is what pytest's default tmp_path factory uses). This
fixture override creates workspace-local temp directories with default
permissions so the complete offline suite runs under workspace-write mode
without per-command sandbox escalation. It changes only *where* temporary
files live, never what the tests assert.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from pathlib import Path

import pytest

_WORKSPACE_TMP = Path(__file__).resolve().parent / ".pytest-work"


def _new_tmp_path() -> Path:
    _WORKSPACE_TMP.mkdir(parents=True, exist_ok=True)
    return _WORKSPACE_TMP / f"tmp-{uuid.uuid4().hex}"


@pytest.fixture()
def tmp_path():
    """Workspace-local writable replacement for pytest's mode-0700 tmp_path."""
    path = _new_tmp_path()
    path.mkdir()
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture()
def outside_repo_tmp_path():
    """Temporary directory outside the repository for boundary tests."""
    path = Path(tempfile.mkdtemp(prefix="hunter-pool-test-"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture(scope="session")
def tmp_path_factory():
    """Workspace-local session-scoped factory with default-permission dirs."""

    class _Factory:
        def mktemp(self, basename: str, numbered: bool = True):
            parent = _WORKSPACE_TMP
            parent.mkdir(parents=True, exist_ok=True)
            if numbered:
                path = parent / f"{basename}{len(list(parent.glob(f'{basename}*')))}"
                path.mkdir()
            else:
                path = parent / basename
                path.mkdir()
            return path

        @property
        def getbasetemp(self) -> Path:
            _WORKSPACE_TMP.mkdir(parents=True, exist_ok=True)
            return _WORKSPACE_TMP

    return _Factory()


@pytest.fixture(scope="session")
def _workspace_tmp():
    return _WORKSPACE_TMP


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session, exitstatus):
    if os.environ.get("HUNTER_KEEP_PYTEST_WORK") != "1":
        shutil.rmtree(_WORKSPACE_TMP, ignore_errors=True)
