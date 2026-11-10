"""
Long-running supervisor for the AI trading team (paper trading only).

  python -m ai_team.runner --data kraken --interval 300 --market-interval 10

Three loops:
* market feed  - live Kraken tickers every ``market_interval`` seconds -> state/market.json
* cycle        - the coordinator invokes all 50 specialists every ``interval`` seconds
                 and submits proposals to the independent risk gateway
* heartbeat    - state/runner.json, so the dashboard can tell running from stalled

The runner has no broker, no credentials and no way to change risk limits.
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from .bus import MessageBus
from .coordinator import Coordinator, build_langgraph
from .data import KrakenPublicData, MarketData, SyntheticMarketData
from .gateway_client import GatewayClient
from .models import utcnow
from .runtime import DEFAULT_UNIVERSE
from .specs import SPECS
from .state import DEFAULT_STATE_DIR, StateStore


MARKET_HISTORY = 720  # points per pair kept for the dashboard (2h at 10s)
# Logs that grow with every cycle are trimmed to their newest lines so a machine
# running non-stop never fills its disk (~360 bus messages per cycle).
TRIM_LIMITS = {"messages.jsonl": 20_000, "decisions.jsonl": 20_000}
TRIM_SLACK = 1.25  # trim only when 25 % over the limit, to avoid rewriting every cycle

log = logging.getLogger("ai_team.runner")


def trim_jsonl(path: Path, keep: int) -> int:
    """Keep the newest ``keep`` lines of a JSONL file; returns how many lines were dropped."""
    if not path.exists():
        return 0
    with path.open("rb") as f:
        count = sum(1 for _ in f)
    if count <= keep * TRIM_SLACK:
        return 0
    with path.open("rb") as f:
        lines = f.readlines()[-keep:]
    tmp = path.with_suffix(".trim")
    tmp.write_bytes(b"".join(lines))
    tmp.replace(path)
    return count - keep


def write_json(path: Path, data: Any) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, default=str))
    tmp.replace(path)


class Runner:
    def __init__(
        self,
        data_kind: str = "kraken",
        gateway_url: str = "http://127.0.0.1:8790",
        state_dir: Path = DEFAULT_STATE_DIR,
        interval: float = 300,
        market_interval: float = 10,
        pairs: list[str] | None = None,
        timeframe: str = "1h",
        use_langgraph: bool = True,
    ) -> None:
        self.data_kind = data_kind
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.interval = interval
        self.market_interval = market_interval
        self.pairs = pairs or list(DEFAULT_UNIVERSE)
        self.timeframe = timeframe
        self.use_langgraph = use_langgraph
        self.gateway = GatewayClient(gateway_url)
        self.state = StateStore(self.state_dir)
        self.stop_event = threading.Event()
        self.runner_file = self.state_dir / "runner.json"
        self.market_file = self.state_dir / "market.json"
        self.status: dict[str, Any] = {
            "pid": None,
            "mode": "paper",
            "live_trading": False,
            "data_source": data_kind,
            "pairs": self.pairs,
            "timeframe": timeframe,
            "interval_s": interval,
            "market_interval_s": market_interval,
            "orchestrator": "langgraph" if use_langgraph else "python",
            "started_at": utcnow(),
            "state": "starting",
            "heartbeat": utcnow(),
            "cycles_completed": 0,
            "cycles_failed": 0,
            "current_cycle": None,
            "last_cycle": None,
            "next_cycle_at": None,
            "last_error": None,
        }
        self._market: dict[str, Any] = (
            json.loads(self.market_file.read_text()) if self.market_file.exists() else {}
        )
        self._market.setdefault("history", {})
        self._lock = threading.Lock()

    # ---------------- helpers ----------------
    def _data(self) -> MarketData:
        return KrakenPublicData() if self.data_kind == "kraken" else SyntheticMarketData()

    def _save_status(self) -> None:
        with self._lock:
            self.status["heartbeat"] = utcnow()
            write_json(self.runner_file, self.status)

    # ---------------- market feed ----------------
    def poll_market(self, source: MarketData | None = None) -> dict[str, Any]:
        src = source or self._data()
        try:
            tickers = src.ticker(self.pairs)  # type: ignore[attr-defined]
            now = utcnow()
            hist = self._market["history"]
            for pair, t in tickers.items():
                series = hist.setdefault(pair, [])
                series.append([now, t["last"]])
                del series[:-MARKET_HISTORY]
            self._market.update(ts=now, ok=True, error=None, source=src.name, pairs=tickers)
        except Exception as e:
            self._market.update(ok=False, error=f"{type(e).__name__}: {e}", error_ts=utcnow())
            log.warning("market feed error: %s", e)
        write_json(self.market_file, self._market)
        return self._market

    def _market_loop(self) -> None:
        src = self._data()
        while not self.stop_event.is_set():
            self.poll_market(src)
            self.stop_event.wait(self.market_interval)

    # ---------------- coordinator cycle ----------------
    def run_cycle(self) -> dict[str, Any]:
        started = time.monotonic()
        started_at = utcnow()
        with self._lock:
            self.status["current_cycle"] = {"started_at": started_at, "pairs": self.pairs}
            self.status["state"] = "cycle-running"
        self._save_status()
        coord = Coordinator(
            self._data(), self.gateway, self.state, MessageBus(self.state.messages_file), universe=self.pairs
        )
        try:
            if self.use_langgraph:
                summary = build_langgraph(coord).invoke(
                    {"pairs": self.pairs, "timeframe": self.timeframe, "submit": True}
                )["summary"]
            else:
                summary = coord.cycle(self.pairs, self.timeframe)
            invoked = {
                a: s["status"]
                for a, s in self.state.agents.items()
                if s.get("updated_at") and s["updated_at"] >= started_at
            }
            counts: dict[str, int] = {}
            for st in invoked.values():
                counts[st] = counts.get(st, 0) + 1
            proposals = [p for v in summary["pairs"].values() for p in v["proposals"]]
            result = {
                "cycle_id": summary["cycle_id"],
                "started_at": started_at,
                "finished_at": utcnow(),
                "duration_s": round(time.monotonic() - started, 1),
                "agents_invoked": len(invoked),
                "agents_expected": len(SPECS),
                "agent_status_counts": counts,
                "proposals": len(proposals),
                "submitted": sum(1 for p in proposals if p.get("coordinator_decision") == "submitted"),
                "approved": sum(1 for p in proposals if (p.get("gateway") or {}).get("approved")),
                "withheld": sum(1 for p in proposals if p.get("coordinator_decision") == "withheld"),
                "gateway_ok": bool(summary.get("gateway", {}).get("ok")),
                "ok": True,
            }
            with self._lock:
                self.status["cycles_completed"] += 1
            log.info(
                "cycle %s done in %ss: %s agents, %s proposals, %s approved",
                result["cycle_id"],
                result["duration_s"],
                result["agents_invoked"],
                result["proposals"],
                result["approved"],
            )
        except Exception as e:
            result = {
                "started_at": started_at,
                "finished_at": utcnow(),
                "ok": False,
                "error": f"{type(e).__name__}: {e}",
            }
            with self._lock:
                self.status["cycles_failed"] += 1
                self.status["last_error"] = result["error"]
            log.exception("cycle failed")
        for name, keep in TRIM_LIMITS.items():
            dropped = trim_jsonl(self.state_dir / name, keep)
            if dropped:
                log.info("trimmed %s lines from %s", dropped, name)
        with self._lock:
            self.status.update(current_cycle=None, last_cycle=result, state="idle")
        self._save_status()
        return result

    def _cycle_loop(self) -> None:
        while not self.stop_event.is_set():
            self.run_cycle()
            with self._lock:
                self.status["next_cycle_at"] = time.strftime(
                    "%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() + self.interval)
                )
            self._save_status()
            self.stop_event.wait(self.interval)

    def _heartbeat_loop(self) -> None:
        while not self.stop_event.is_set():
            self._save_status()
            self.stop_event.wait(5)

    # ---------------- lifecycle ----------------
    def run_forever(self) -> None:
        import os

        self.status["pid"] = os.getpid()
        self._save_status()
        threads = [
            threading.Thread(target=self._market_loop, name="market", daemon=True),
            threading.Thread(target=self._heartbeat_loop, name="heartbeat", daemon=True),
            threading.Thread(target=self._cycle_loop, name="cycle", daemon=True),
        ]
        for t in threads:
            t.start()
        log.info(
            "runner started: data=%s pairs=%s interval=%ss (paper only)",
            self.data_kind,
            self.pairs,
            self.interval,
        )
        self.stop_event.wait()
        log.info("runner stopping")
        with self._lock:
            self.status["state"] = "stopped"
        self._save_status()

    def stop(self, *_: Any) -> None:
        self.stop_event.set()


def setup_logging(state_dir: Path) -> None:
    handler = RotatingFileHandler(state_dir / "runner.log", maxBytes=2_000_000, backupCount=2)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    root.addHandler(logging.StreamHandler())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ai_team.runner")
    ap.add_argument("--data", choices=["kraken", "synthetic"], default="kraken")
    ap.add_argument("--gateway", default="http://127.0.0.1:8790")
    ap.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    ap.add_argument("--interval", type=float, default=300, help="seconds between coordinator cycles")
    ap.add_argument("--market-interval", type=float, default=10, help="seconds between price updates")
    ap.add_argument("--pairs", default=",".join(DEFAULT_UNIVERSE))
    ap.add_argument("--timeframe", default="1h")
    ap.add_argument("--no-langgraph", action="store_true")
    ap.add_argument("--once", action="store_true", help="run one market poll and one cycle, then exit")
    a = ap.parse_args(argv)
    a.state_dir.mkdir(parents=True, exist_ok=True)
    setup_logging(a.state_dir)
    r = Runner(
        a.data,
        a.gateway,
        a.state_dir,
        a.interval,
        a.market_interval,
        a.pairs.split(","),
        a.timeframe,
        not a.no_langgraph,
    )
    if a.once:
        r.poll_market()
        return 0 if r.run_cycle().get("ok") else 1
    signal.signal(signal.SIGTERM, r.stop)
    signal.signal(signal.SIGINT, r.stop)
    r.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
