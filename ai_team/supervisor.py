"""
Keep the whole paper-trading system running (Windows, macOS, Linux).

  python -m ai_team.supervisor [--data kraken] [--dashboard-host 127.0.0.1]

Starts the risk gateway, the runner and the dashboard as child processes and
restarts any of them that exits, with an increasing back-off so a component that
keeps crashing does not spin. The gateway always binds to 127.0.0.1; only the
read-only dashboard can be opened to the local network. Writes its own status to
state/supervisor.json and stops all children on SIGTERM / Ctrl+C.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from .data import TIMEFRAME_MINUTES
from .models import utcnow
from .runtime import DEFAULT_TIMEFRAME
from .state import DEFAULT_STATE_DIR


ROOT = Path(__file__).resolve().parents[1]
log = logging.getLogger("ai_team.supervisor")

BACKOFF_START_S = 2.0
BACKOFF_MAX_S = 300.0
HEALTHY_AFTER_S = 120.0  # a child that ran this long resets its back-off


class Child:
    def __init__(self, name: str, args: list[str], state_dir: Path) -> None:
        self.name = name
        self.args = args
        self.state_dir = state_dir
        self.proc: subprocess.Popen | None = None
        self.restarts = 0
        self.backoff = BACKOFF_START_S
        self.started_at: float | None = None
        self.next_start = 0.0
        self.last_exit: int | None = None

    def start(self) -> None:
        out = (self.state_dir / f"{self.name}.out").open("ab")  # handed to the child, closed below
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # Windows only: no console window per child
        self.proc = subprocess.Popen(
            [sys.executable, *self.args], cwd=ROOT, stdout=out, stderr=out, creationflags=flags
        )
        out.close()
        self.started_at = time.monotonic()
        log.info("%s started (pid %s)", self.name, self.proc.pid)

    def poll(self) -> None:
        now = time.monotonic()
        if self.proc is None:
            if now >= self.next_start:
                self.start()
            return
        code = self.proc.poll()
        if code is None:
            if self.started_at and now - self.started_at > HEALTHY_AFTER_S:
                self.backoff = BACKOFF_START_S
            return
        self.last_exit = code
        self.restarts += 1
        log.warning("%s exited with code %s; restarting in %.0fs", self.name, code, self.backoff)
        self.proc = None
        self.next_start = now + self.backoff
        self.backoff = min(self.backoff * 2, BACKOFF_MAX_S)

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            log.info("%s stopped", self.name)

    def status(self) -> dict[str, Any]:
        running = self.proc is not None and self.proc.poll() is None
        return {
            "running": running,
            "pid": self.proc.pid if running and self.proc else None,
            "restarts": self.restarts,
            "last_exit_code": self.last_exit,
        }


def build_children(a: argparse.Namespace, state_dir: Path) -> list[Child]:
    gw = f"http://127.0.0.1:{a.gateway_port}"
    return [
        Child(
            "gateway",
            ["-m", "risk_gateway.server", "--port", str(a.gateway_port), "--data", a.data],
            state_dir,
        ),
        Child(
            "runner",
            [
                "-m",
                "ai_team.runner",
                "--data",
                a.data,
                "--interval",
                str(a.interval),
                "--timeframe",
                a.timeframe,
                "--gateway",
                gw,
                "--state-dir",
                str(state_dir),
            ],
            state_dir,
        ),
        Child(
            "dashboard",
            [
                "-m",
                "ai_team.dashboard.server",
                "--host",
                a.dashboard_host,
                "--port",
                str(a.dashboard_port),
                "--gateway",
                gw,
                "--state-dir",
                str(state_dir),
            ],
            state_dir,
        ),
    ]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ai_team.supervisor")
    ap.add_argument("--data", choices=["kraken", "synthetic"], default="kraken")
    ap.add_argument("--interval", type=float, default=300)
    ap.add_argument("--timeframe", default=DEFAULT_TIMEFRAME, choices=sorted(TIMEFRAME_MINUTES))
    ap.add_argument("--gateway-port", type=int, default=8790)
    ap.add_argument("--dashboard-port", type=int, default=8800)
    ap.add_argument(
        "--dashboard-host",
        default="127.0.0.1",
        help="0.0.0.0 opens the read-only dashboard to the local network",
    )
    ap.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    a = ap.parse_args(argv)
    state_dir: Path = a.state_dir
    state_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(state_dir / "supervisor.log", maxBytes=1_000_000, backupCount=2)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler, logging.StreamHandler()])

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    if hasattr(signal, "SIGBREAK"):  # Windows console close / Ctrl+Break
        signal.signal(signal.SIGBREAK, lambda *_: stop.set())

    children = build_children(a, state_dir)
    status_file = state_dir / "supervisor.json"
    # Portable stop request: "system stop" creates this file (signals are unreliable on Windows).
    stop_file = state_dir / "supervisor.stop"
    stop_file.unlink(missing_ok=True)
    started = utcnow()
    log.info(
        "supervisor starting (PAPER TRADING ONLY, data=%s, dashboard=%s:%s)",
        a.data,
        a.dashboard_host,
        a.dashboard_port,
    )
    try:
        while not stop.is_set():
            for c in children:
                c.poll()
                if (
                    c.name == "gateway"
                    and c.proc is not None
                    and c.started_at
                    and time.monotonic() - c.started_at < 3
                ):
                    stop.wait(3)  # give the gateway a head start; the runner fails closed meanwhile
            tmp = status_file.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "started_at": started,
                        "heartbeat": utcnow(),
                        "dashboard_host": a.dashboard_host,
                        "children": {c.name: c.status() for c in children},
                    }
                )
            )
            tmp.replace(status_file)
            if stop_file.exists():
                log.info("stop requested via %s", stop_file.name)
                stop.set()
            stop.wait(5)
    finally:
        for c in reversed(children):
            c.stop()
        stop_file.unlink(missing_ok=True)
        try:
            data = json.loads(status_file.read_text())
            data.update(stopped_at=utcnow(), children={c.name: c.status() for c in children})
            status_file.write_text(json.dumps(data))
        except (OSError, ValueError):
            pass
        log.info("supervisor stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
