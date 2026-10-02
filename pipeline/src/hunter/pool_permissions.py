"""Permission hardening for secret directories (Windows ACL / POSIX mode).

On Windows, removes inherited ACL entries and retains only:
  - current user (SID)
  - SYSTEM
  - Administrators

On POSIX, sets mode 0700 (directory) / 0600 (file).

verify_secret_directory returns True when the path complies; otherwise False.
secure_new_secret_directory creates a new directory with the hardened ACL/mode.

Existing directories are never modified — only verified.
"""
from __future__ import annotations

import os
from pathlib import Path


def verify_secret_directory(path: Path) -> bool:
    """Check that *path* is a hardened secret directory.

    On Windows, verifies the DACL contains NO entry for Authenticated Users
    or Users.  On POSIX, checks that the directory mode is 0o700.
    """
    if not path.is_dir():
        return False
    if os.name == "nt":
        return _verify_nt_acls(path)
    return _verify_posix_mode(path, 0o700)


def secure_new_secret_directory(path: Path) -> None:
    """Create *path* with hardened permissions.

    The directory must NOT already exist.
    """
    if path.exists():
        raise PermissionError(
            f"refusing to re-permission existing path: {path}"
        )
    if os.name == "nt":
        path.mkdir(parents=True, exist_ok=False)
        _harden_nt_directory(path)
    else:
        path.mkdir(parents=True, exist_ok=False, mode=0o700)


def secure_new_secret_file(path: Path) -> None:
    """Create *path* as a hardened file (POSIX 0o600, Windows hardened)."""
    if os.name == "nt":
        path.touch()
        _harden_nt_directory(path)
    else:
        path.touch(mode=0o600, exist_ok=False)


# ---------------------------------------------------------------------------
# Windows ACL helpers
# ---------------------------------------------------------------------------

def _verify_nt_acls(path: Path) -> bool:
    import subprocess
    # icacls emits locale-encoded (e.g. GBK) summary lines after the ACE
    # list; strict decoding crashes the capture thread and yields no stdout.
    result = subprocess.run(
        ["icacls", str(path)],
        capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=10,
    )
    if result.stdout is None:
        return False
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("NT AUTHORITY\\Authenticated Users"):
            if "F" in stripped or "M" in stripped:
                return False
        if stripped.startswith("BUILTIN\\Users"):
            if "F" in stripped or "M" in stripped:
                return False
    return True


def _harden_nt_directory(path: Path) -> None:
    import subprocess
    sid = _current_sid()
    subprocess.run(
        ["icacls", str(path), "/inheritance:r",
         "/grant", f"*{sid}:(OI)(CI)F",
         "/grant", "SYSTEM:(OI)(CI)F",
         "/grant", "BUILTIN\\Administrators:(OI)(CI)F"],
        check=True, capture_output=True, timeout=10,
    )


def _current_sid() -> str:
    import subprocess
    result = subprocess.run(
        ["powershell", "-Command", "(Get-WmiObject Win32_UserAccount -Filter \"Name='$env:USERNAME' AND Domain='$env:USERDOMAIN'\").SID"],
        capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=10,
    )
    sid = result.stdout.strip()
    if sid.startswith("S-1-"):
        return sid
    raise RuntimeError(f"Could not determine current SID from:\n{result.stdout}")
