"""Atomic file replacement that tolerates short-lived locks on Windows."""

from __future__ import annotations

import time
from pathlib import Path


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
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding=encoding)
    replace_with_retry(tmp, path)
