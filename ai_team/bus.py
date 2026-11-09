"""Message bus: every hand-off between agents is recorded so workflows are auditable."""

from __future__ import annotations

import json
import threading
from pathlib import Path

from .models import Message


class MessageBus:
    def __init__(self, log_file: Path | None) -> None:
        self.log_file = log_file
        self.messages: list[Message] = []
        self._lock = threading.Lock()

    def publish(
        self, sender: str, recipient: str, topic: str, payload: dict, workflow: str | None = None
    ) -> Message:
        msg = Message(sender, recipient, topic, payload, workflow)
        with self._lock:
            self.messages.append(msg)
            if self.log_file:
                self.log_file.parent.mkdir(parents=True, exist_ok=True)
                with self.log_file.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(msg.to_dict(), default=str) + "\n")
        return msg

    def chain(self, workflow: str) -> list[tuple[str, str]]:
        return [
            (m.sender, m.recipient) for m in self.messages if m.workflow == workflow and m.topic == "handoff"
        ]
