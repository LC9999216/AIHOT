"""ACL verification must survive localized icacls output.

icacls on non-English Windows appends locale-encoded (e.g. GBK) summary
lines after the ACE list. Strict UTF-8 capture crashed the reader thread,
leaving stdout=None, and the verify call died with AttributeError instead
of returning a verdict.
"""
from __future__ import annotations

import os
import subprocess

import pytest

from hunter.pool_permissions import verify_secret_directory

pytestmark = pytest.mark.skipif(os.name != "nt", reason="NT ACL path")

GBK_TAIL = "已成功处理 1 个文件; 处理 0 个文件时失败".encode("gbk")
SAFE_ACL_BYTES = (
    "BUILTIN\\Administrators:(I)(OI)(CI)(F)\n"
    "NT AUTHORITY\\SYSTEM:(I)(OI)(CI)(F)\n"
    "LAPTOP-NANM4BL1\\HP:(I)(OI)(CI)(F)\n"
).encode("ascii")
UNSAFE_ACL_BYTES = b"NT AUTHORITY\\Authenticated Users:(I)(OI)(CI)(F)\n"


class _FakeIcacls:
    """Mimic subprocess.run honoring text/encoding/errors like the real one.

    If the caller requests text without an explicit encoding, reproduce the
    real crash mode: the reader thread fails and stdout arrives as None.
    """

    def __init__(self, stdout_bytes: bytes) -> None:
        self._stdout_bytes = stdout_bytes

    def __call__(self, argv, **kwargs):
        encoding = kwargs.get("encoding")
        if kwargs.get("text") and encoding is None:
            return subprocess.CompletedProcess(argv, 0, stdout=None, stderr=None)
        errors = kwargs.get("errors", "strict")
        text = self._stdout_bytes.decode(encoding, errors)
        return subprocess.CompletedProcess(argv, 0, stdout=text, stderr="")


def test_unsafe_ace_with_localized_tail_reports_unsafe(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "run", _FakeIcacls(UNSAFE_ACL_BYTES + GBK_TAIL))
    assert verify_secret_directory(tmp_path) is False


def test_hardened_acl_with_localized_tail_reports_safe(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "run", _FakeIcacls(SAFE_ACL_BYTES + GBK_TAIL))
    assert verify_secret_directory(tmp_path) is True


def test_unreadable_output_fails_closed(tmp_path, monkeypatch):
    def no_stdout(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout=None, stderr=None)

    monkeypatch.setattr(subprocess, "run", no_stdout)
    assert verify_secret_directory(tmp_path) is False
