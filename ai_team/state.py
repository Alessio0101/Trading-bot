"""Persistent state read by the dashboard: per-agent status, decision log."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from .models import Report, Signal, utcnow
from .specs import BY_ID, CATEGORIES, SPECS


DEFAULT_STATE_DIR = Path(__file__).resolve().parent / "state"


class StateStore:
    def __init__(self, state_dir: Path = DEFAULT_STATE_DIR) -> None:
        self.dir = Path(state_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.agents_file = self.dir / "agents.json"
        self.decisions_file = self.dir / "decisions.jsonl"
        self.messages_file = self.dir / "messages.jsonl"
        self.reports_dir = self.dir / "reports"
        self._lock = threading.Lock()
        self.agents: dict[str, dict[str, Any]] = self._load()

    def _blank(self, spec) -> dict[str, Any]:
        return {
            "id": spec.id,
            "number": spec.number,
            "name": spec.name,
            "category": CATEGORIES[spec.category],
            "capability": spec.status,
            "gap": spec.gap,
            "status": "idle",
            "online": spec.status != "needs_integration",
            "current_task": None,
            "latest_analysis": None,
            "signals": [],
            "errors": [],
            "performance": {"runs": 0, "ok": 0, "errors": 0, "needs_integration": 0, "avg_ms": 0.0},
            "risk": {"submitted": 0, "approved": 0, "rejected": 0, "last": None},
            "updated_at": None,
        }

    def _load(self) -> dict[str, dict[str, Any]]:
        data = json.loads(self.agents_file.read_text()) if self.agents_file.exists() else {}
        for spec in SPECS:
            data.setdefault(spec.id, self._blank(spec))
            data[spec.id]["capability"] = spec.status
            data[spec.id]["gap"] = spec.gap
        return data

    def save(self) -> None:
        tmp = self.agents_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.agents, indent=1, default=str))
        tmp.replace(self.agents_file)

    def start(self, agent_id: str, objective: str) -> None:
        with self._lock:
            a = self.agents[agent_id]
            a.update(status="running", current_task=objective, updated_at=utcnow())
            self.save()

    def finish(self, report: Report) -> None:
        self.reports_dir.mkdir(exist_ok=True)
        (self.reports_dir / f"{report.agent_id}.json").write_text(json.dumps(report.to_dict(), default=str))
        with self._lock:
            a = self.agents[report.agent_id]
            perf = a["performance"]
            perf["runs"] += 1
            key = {"ok": "ok", "error": "errors"}.get(report.status, "needs_integration")
            perf[key] += 1
            if report.duration_ms is not None:
                perf["avg_ms"] = round(
                    perf["avg_ms"] + (report.duration_ms - perf["avg_ms"]) / perf["runs"], 1
                )
            perf["success_rate"] = round(perf["ok"] / perf["runs"], 3)
            a.update(
                status={"ok": "idle", "error": "error"}.get(report.status, "offline"),
                online=report.status == "ok",
                current_task=None,
                latest_analysis=report.summary,
                signals=[s.to_dict() for s in report.signals][-5:],
                updated_at=report.finished_at,
            )
            if report.errors or report.status == "error":
                a["errors"] = (a["errors"] + [{"ts": report.finished_at, "error": report.summary}])[-5:]
            self.save()

    def load_report(self, agent_id: str) -> Report | None:
        """Latest stored report of an agent, used to hand off between separate CLI runs."""
        f = self.reports_dir / f"{agent_id}.json"
        if not f.exists():
            return None
        d = json.loads(f.read_text())
        d["signals"] = [Signal(**x) for x in d["signals"]]
        return Report(**d)

    def risk_result(self, agent_ids: list[str], approved: bool, summary: str) -> None:
        with self._lock:
            for aid in agent_ids:
                if aid in self.agents:
                    r = self.agents[aid]["risk"]
                    r["submitted"] += 1
                    r["approved" if approved else "rejected"] += 1
                    r["last"] = summary
            self.save()

    def record_decision(self, kind: str, **details: Any) -> dict:
        entry = {"ts": utcnow(), "type": kind, **details}
        with self._lock, self.decisions_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
        return entry

    def decisions(self, limit: int = 200) -> list[dict]:
        if not self.decisions_file.exists():
            return []
        return [json.loads(x) for x in self.decisions_file.read_text().splitlines()[-limit:]]


def spec_of(agent_id: str):
    return BY_ID[agent_id]
