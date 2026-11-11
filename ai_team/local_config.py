"""
Per-computer settings that must never enter the repository: API keys and bot tokens.

They live in ~/.ai-trading-desk/<name>.json (override with AI_TEAM_CONFIG_DIR), outside the
repository, so downloading a new version of the bot keeps them. The Claude Code agents are
denied reads of this directory (.claude/settings.json).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .fsutil import atomic_write_text


def config_dir() -> Path:
    return Path(os.environ.get("AI_TEAM_CONFIG_DIR") or Path.home() / ".ai-trading-desk")


def path_of(name: str) -> Path:
    return config_dir() / f"{name}.json"


def load(name: str) -> dict[str, Any] | None:
    try:
        data = json.loads(path_of(name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def save(name: str, data: dict[str, Any]) -> Path:
    path = path_of(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(data))
    try:
        path.chmod(0o600)  # owner only, where the OS supports it
    except OSError:
        pass
    return path
