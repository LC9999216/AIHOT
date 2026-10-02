"""Cross-process single-instance locks with same-process re-entry.

Review round 2 item 六.4: RuntimeStore, OutboxStore, and the Stage 2 runner
need single-instance (or equivalent concurrency) control.

Design:
- One lock file per protected resource.
- Across processes the lock is EXCLUSIVE: the underlying byte-range lock is
  held by the OS and is released automatically if the holder dies, so a
  crashed process never leaves a stale lock behind.
- Within one process the lock is RE-ENTRANT with a refcount: tests and the
  runner legitimately open a second store on the same path in-process.

``LockHeldError`` fails closed: callers must not silently continue.
"""

from __future__ import annotations

import errno
import os
import threading
import time
from pathlib import Path
from typing import Dict

try:  # POSIX
    import fcntl
    _HAVE_FCNTL = True
except ImportError:  # Windows
    import msvcrt
    _HAVE_FCNTL = False


class LockHeldError(RuntimeError):
    """Another process holds the lock."""


class ProcessFileLock:
    """Exclusive across processes, re-entrant within the current process."""

    _held: Dict[str, int] = {}
    _held_mutex = threading.Lock()

    def __init__(self, path: "str | Path"):
        self.path = Path(path)
        self._fd: "int | None" = None
        self._key = str(self.path.resolve(strict=False)).lower()

    def acquire(self, timeout: float = 0.0, reentrant: bool = True) -> None:
        """Acquire the lock, raising LockHeldError if another process has it.

        Args:
            timeout: 0.0 (default) -> one attempt; >0 -> spin up to ``timeout`` seconds.
            reentrant: True (default) allows same-process re-entry without a real OS
                       lock attempt; False forces a new OS lock even in the same process.
        """
        with ProcessFileLock._held_mutex:
            count = ProcessFileLock._held.get(self._key, 0)
            if count > 0 and reentrant:
                ProcessFileLock._held[self._key] = count + 1
                return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o600)
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            try:
                if _HAVE_FCNTL:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                else:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    os.close(fd)
                    raise
                if time.monotonic() >= deadline:
                    os.close(fd)
                    raise LockHeldError(
                        f"another process holds the lock {self.path}"
                    ) from exc
                time.sleep(0.05)
        with ProcessFileLock._held_mutex:
            self._fd = fd
            ProcessFileLock._held[self._key] = ProcessFileLock._held.get(self._key, 0) + 1

    def release(self) -> None:
        with ProcessFileLock._held_mutex:
            count = ProcessFileLock._held.get(self._key, 0)
            if count <= 0:
                return
            ProcessFileLock._held[self._key] = count - 1
            if count - 1 > 0:
                return
            fd = self._fd
            self._fd = None
        if fd is not None:
            try:
                if _HAVE_FCNTL:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                else:
                    try:
                        os.lseek(fd, 0, os.SEEK_SET)
                        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                    except OSError:
                        pass
            finally:
                os.close(fd)

    def __enter__(self) -> "ProcessFileLock":
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()


__all__ = ["ProcessFileLock", "LockHeldError"]
