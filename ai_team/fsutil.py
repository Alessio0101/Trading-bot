"""Atomic file replacement that tolerates short-lived locks on Windows."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import IO


REPLACE_ATTEMPTS = 100
REPLACE_DELAY_S = 0.05


def replace_with_retry(tmp: Path, path: Path) -> None:
    """``tmp.replace(path)``, retried while another process briefly holds ``path``.

    On Windows, replacing a file fails with ``PermissionError`` (WinError 5) while any
    process has it open, for example the dashboard reading ``agents.json`` or an
    antivirus scan. Those reads take milliseconds, so retrying for up to ~5 s is enough.
    """
    for attempt in range(REPLACE_ATTEMPTS):
        try:
            tmp.replace(path)
            return
        except PermissionError:
            if attempt == REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(REPLACE_DELAY_S)


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    # A temporary name unique to this process and thread, so two writers never
    # replace (and so delete) each other's half-written file.
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_text(text, encoding=encoding)
        replace_with_retry(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


class AlreadyRunning(RuntimeError):
    """Another process holds the single-instance lock."""


def acquire_instance_lock(path: Path) -> IO[bytes]:
    """Hold an exclusive OS lock on ``path`` for the life of this process.

    The lock is released automatically when the process exits, even after a crash,
    so a stale lock file never blocks a restart. Raises AlreadyRunning when another
    live process holds it. Keep the returned handle referenced.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        fh.close()
        raise AlreadyRunning(f"{path} is held by another process") from e
    fh.seek(0)
    fh.truncate()
    fh.write(str(os.getpid()).encode())
    fh.flush()
    return fh
