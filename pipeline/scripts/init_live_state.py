"""Initialize the external live-state directory for Stage 2 validation.

Create (default): populate missing data, harden pool directories.
Verify: read-only check of existing state — report missing/mismatched/permissions.
No overwrite on hash mismatch. No refresh without explicit sub-command/re-authorization.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

from hunter.pool_permissions import (
    secure_new_secret_directory,
    verify_secret_directory,
)

EXTERNAL_STATE_DEFAULT = Path("D:/AI/Free token/Free Token Hunter-stage2-live-state")


def _copy_file(src: Path, dst: Path) -> None:
    """Atomically copy *src* to *dst* via a temp file in the same directory."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(dst.parent), prefix=".init-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            data = src.read_bytes()
            f.write(data)
            f.flush()
            os.fsync(fd)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, dst)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def init(
    worktree_root: Path,
    state_root: Path | None = None,
    mode: str = "create",
) -> str:
    """Run initialization or verification.

    Returns a JSON string with results.
    """
    state_root = state_root or EXTERNAL_STATE_DEFAULT
    errors: list[str] = []
    missing: list[str] = []
    copied: list[str] = []
    mismatched: list[str] = []

    pool_production = state_root / "pool" / "production"
    pool_staging = state_root / "pool" / "staging"
    live_data = state_root / "data"
    clients_dir = state_root / "clients"

    if mode in ("create", "init"):
        pool_production_created = not pool_production.exists()
        for d in (pool_staging, live_data, clients_dir):
            d.mkdir(parents=True, exist_ok=True)
        if pool_production_created:
            secure_new_secret_directory(pool_production)
        else:
            pool_production.mkdir(parents=True, exist_ok=True)
        for name in ("providers.json", "candidates.json", "evidence.json", "history.jsonl"):
            src = worktree_root / "data" / name
            dst = live_data / name
            if not src.is_file():
                continue
            if dst.is_file():
                if _sha256(src) == _sha256(dst):
                    continue
                mismatched.append(name)
                continue
            try:
                _copy_file(src, dst)
                copied.append(name)
            except OSError as exc:
                # Clean up partial destination
                dst.unlink(missing_ok=True)
                errors.append(f"copy_failed:{name}:{exc}")
    elif mode == "verify":
        if not pool_production.is_dir():
            errors.append("pool/production_not_found")
        elif not verify_secret_directory(pool_production):
            errors.append("permissions_unsafe")
        for name in ("providers.json", "candidates.json", "evidence.json", "history.jsonl"):
            src = worktree_root / "data" / name
            dst = live_data / name
            if not dst.is_file():
                missing.append(name)
                continue
            if _sha256(src) != _sha256(dst):
                mismatched.append(name)

    result = {
        "state_root": str(state_root),
        "copied": copied,
        "missing": missing,
        "mismatched": mismatched,
        "errors": errors,
    }
    if errors:
        return json.dumps(result)
    return json.dumps(result)


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="init-live-state")
    parser.add_argument("--worktree", type=Path, default=Path.cwd())
    parser.add_argument("--state", type=Path, default=EXTERNAL_STATE_DEFAULT)
    parser.add_argument("--mode", choices=("create", "verify"), default="create")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    result = json.loads(init(args.worktree, args.state, args.mode))
    if args.mode == "verify" and args.dry_run:
        print(json.dumps(result, indent=2))
        return 0 if not result.get("errors") else 1
    if args.mode == "verify":
        if result.get("errors") or result.get("missing") or result.get("mismatched"):
            print(json.dumps(result, indent=2))
            return 1
    print(json.dumps(result, indent=2))
    return 0 if not result.get("errors") else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
