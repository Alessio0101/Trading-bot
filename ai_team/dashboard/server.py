"""
Live dashboard for the AI trading team (read-only, paper trading only).

  python -m ai_team.dashboard.server --port 8800 [--gateway http://127.0.0.1:8790]

Every number shown comes from the running system: the runner's state files
(agents, signals, market feed, coordinator decisions, bus messages, logs) and the
independent risk gateway's HTTP API (health, limits, portfolio, decisions).
Nothing is mocked; a missing source is reported as unavailable.

Endpoints: /  /api/state  /api/stream (Server-Sent Events)  /api/agent/<id>  /api/logs
The dashboard cannot submit proposals or change anything.
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import pairwise
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from ..gateway_client import GatewayClient
from ..integrations import all_status
from ..specs import BY_ID, CATEGORIES, SPECS, WORKFLOWS
from ..state import DEFAULT_STATE_DIR, SIGNAL_AGENTS, StateStore
from ..track_record import HORIZON_BARS, SignalLedger


HTML_FILE = Path(__file__).parent / "index.html"
RUNNER_STALE_S = 30
MARKET_STALE_S = 60
STREAM_INTERVAL_S = 3.0


def _age_s(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        return (datetime.now(UTC) - datetime.fromisoformat(ts)).total_seconds()
    except ValueError:
        return None


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _tail_lines(path: Path, n: int, max_bytes: int = 1_000_000) -> list[str]:
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read().decode("utf-8", "replace")
    except OSError:
        return []
    lines = data.splitlines()
    if size > max_bytes:
        lines = lines[1:]  # first line is probably partial
    return lines[-n:]


def _downsample(points: list, limit: int) -> list:
    if len(points) <= limit:
        return points
    step = len(points) / limit
    return [points[int(i * step)] for i in range(limit - 1)] + [points[-1]]


def _gateway(gw: GatewayClient) -> dict[str, Any]:
    health = gw.health()
    out: dict[str, Any] = {"health": health, "limits": None, "portfolio": None, "decisions": None}
    if not health.get("ok"):
        return out
    for key, path in (("limits", "/limits"), ("portfolio", "/portfolio"), ("decisions", "/decisions")):
        try:
            out[key] = gw._call("GET", path)
        except OSError as e:
            out[f"{key}_error"] = str(e)
    return out


def _collaboration(state_dir: Path) -> dict[str, Any]:
    lines = _tail_lines(state_dir / "messages.jsonl", 3000)
    edges: dict[str, int] = {}
    recent = []
    for line in lines:
        try:
            m = json.loads(line)
        except ValueError:
            continue
        if m.get("topic") == "handoff":
            key = f"{m['sender']}→{m['recipient']}"
            edges[key] = edges.get(key, 0) + 1
            recent.append(
                {k: m.get(k) for k in ("sent_at", "sender", "recipient", "workflow")}
                | {"summary": (m.get("payload") or {}).get("summary")}
            )
    workflows = []
    for name, chain in WORKFLOWS.items():
        counts = [edges.get(f"{a}→{b}", 0) for a, b in pairwise(chain)]
        hops = [{"from": a, "to": b, "count": c} for (a, b), c in zip(pairwise(chain), counts, strict=True)]
        workflows.append({"name": name, "chain": chain, "hops": hops, "active": all(c > 0 for c in counts)})
    return {"workflows": workflows, "recent_handoffs": recent[-40:][::-1], "messages_scanned": len(lines)}


def _performance(store: StateStore, gw: dict[str, Any], decisions: list[dict]) -> dict[str, Any]:
    pf = gw.get("portfolio") or {}
    realised = pf.get("realised") or []
    wins = [r for r in realised if r["pnl"] > 0]
    gw_decisions = gw.get("decisions") or []
    submitted = [d for d in gw_decisions if d.get("origin") != "gateway-protective-exit"]
    start_equity = (gw.get("limits") or {}).get("starting_equity")
    system = {
        "equity": pf.get("equity"),
        "starting_equity": start_equity,
        "total_return_pct": (pf["equity"] / start_equity - 1) * 100
        if pf.get("equity") and start_equity
        else None,
        "realised_pnl": sum(r["pnl"] for r in realised),
        "unrealised_pnl": sum(u["pnl"] for u in (pf.get("unrealised") or {}).values()),
        "closed_trades": len(realised),
        "win_rate": len(wins) / len(realised) if realised else None,
        "proposals_to_gateway": len(submitted),
        "approved": sum(1 for d in submitted if d.get("approved")),
        "rejected": sum(1 for d in submitted if not d.get("approved")),
        "protective_exits": sum(1 for d in gw_decisions if d.get("origin") == "gateway-protective-exit"),
    }
    reviews = [d for d in decisions if d.get("type") == "strategy_review"]
    latest_review: dict[str, Any] = {}
    for d in reviews:
        latest_review[d["pair"]] = {
            "ts": d["ts"],
            "backtest": d.get("backtest") or {},
            "verdicts": d.get("verdicts") or {},
        }
    agents = []
    for a in store.agents.values():
        p = a["performance"]
        agents.append(
            {
                "id": a["id"],
                "number": a["number"],
                "name": a["name"],
                "category": a["category"],
                "runs": p["runs"],
                "ok": p["ok"],
                "errors": p["errors"],
                "needs_integration": p["needs_integration"],
                "success_rate": p.get("success_rate"),
                "avg_ms": p["avg_ms"],
                "risk": a["risk"],
            }
        )
    ledger = SignalLedger(store.dir / "signal_ledger.json")
    return {
        "system": system,
        "strategies_by_pair": latest_review,
        "agents": sorted(agents, key=lambda x: x["number"]),
        "track_record": ledger.scores(),
        "signals_pending": ledger.pending(),
        "horizon_bars": HORIZON_BARS,
    }


def _alerts(
    runner: dict | None, market: dict | None, gw: dict[str, Any], agents: list[dict], pf: dict | None
) -> list[dict[str, str]]:
    alerts = []

    def add(level: str, text: str) -> None:
        alerts.append({"level": level, "text": text})

    if not runner:
        add("critical", "Runner has never started: no coordinator cycles are running.")
    elif runner.get("state") == "stopped":
        add("critical", "Runner is stopped.")
    elif (_age_s(runner.get("heartbeat")) or 1e9) > RUNNER_STALE_S:
        add("critical", f"Runner heartbeat is stale ({int(_age_s(runner.get('heartbeat')) or 0)} s).")
    if runner and runner.get("last_cycle") and not runner["last_cycle"].get("ok"):
        add("error", f"Last coordinator cycle failed: {runner['last_cycle'].get('error')}")
    if not gw["health"].get("ok"):
        add("critical", "Risk gateway unreachable: every proposal is rejected (fail closed).")
    elif gw["health"].get("mode") != "paper" or gw["health"].get("live_trading") is not False:
        add("critical", "Risk gateway is not in paper-only mode.")
    lpc = gw["health"].get("last_price_check") or {}
    if lpc.get("ok") is False:
        add("warning", f"Gateway price check failed: {lpc.get('error')}")
    if not market:
        add("warning", "No market data received yet.")
    else:
        if market.get("ok") is False:
            add("warning", f"Kraken feed error: {market.get('error')}")
        age = _age_s(market.get("ts"))
        if age is None or age > MARKET_STALE_S:
            add("warning", f"Market data is stale ({int(age or 0)} s old).")
    errs = [a["id"] for a in agents if a["status"] == "error"]
    if errs:
        add("error", f"{len(errs)} agent(s) in error: {', '.join(errs[:6])}")
    if pf and pf.get("stale_prices"):
        add("warning", f"Portfolio valued with stale prices for {pf['stale_prices']}")
    if pf and pf.get("drawdown_pct") is not None and pf["drawdown_pct"] <= -0.05:
        add("warning", f"Paper drawdown {pf['drawdown_pct']:.1%}")
    return alerts


def collect(state_dir: Path, gateway: GatewayClient) -> dict[str, Any]:
    store = StateStore(state_dir)
    runner = _read_json(state_dir / "runner.json")
    market = _read_json(state_dir / "market.json")
    verification = _read_json(state_dir / "verification.json")
    gw = _gateway(gateway)
    decisions = store.decisions(400)
    pf = gw.get("portfolio")

    agents = []
    for a in sorted(store.agents.values(), key=lambda x: x["number"]):
        spec = BY_ID[a["id"]]
        agents.append(
            {
                **a,
                "access": spec.access,
                "integrations": list(spec.integrations),
                "receives_from": list(spec.receives_from),
                "sends_to": list(spec.sends_to),
                "mission": spec.mission,
                "signal_agent": a["id"] in SIGNAL_AGENTS,
            }
        )

    if runner:
        runner = {
            **runner,
            "heartbeat_age_s": _age_s(runner.get("heartbeat")),
            "alive": runner.get("state") != "stopped"
            and (_age_s(runner.get("heartbeat")) or 1e9) <= RUNNER_STALE_S,
        }
    if market:
        market = {
            **market,
            "age_s": _age_s(market.get("ts")),
            "history": {p: _downsample(h, 180) for p, h in (market.get("history") or {}).items()},
        }
    if pf:
        pf = {
            **pf,
            "history": _downsample(pf.get("history") or [], 300),
            "fills": (pf.get("fills") or [])[-100:][::-1],
            "realised": (pf.get("realised") or [])[-100:][::-1],
        }
        gw["portfolio"] = pf

    gw_decisions = gw.get("decisions") or []
    coordinator = [d for d in decisions if d.get("type") != "gateway"][::-1][:120]
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "mode": {"paper": True, "live_trading": False},
        "runner": runner,
        "market": market,
        "agents": agents,
        "categories": list(CATEGORIES.values()),
        "signals": {"board": store.signals, "agents": SIGNAL_AGENTS},
        "gateway": {
            "health": gw["health"],
            "limits": gw.get("limits"),
            "portfolio": pf,
            "decisions": gw_decisions[::-1][:150],
            "blocked": [d for d in gw_decisions if not d.get("approved")][::-1][:100],
            "errors": {k: v for k, v in gw.items() if k.endswith("_error")},
        },
        "coordinator": {
            "decisions": coordinator,
            "withheld": [d for d in coordinator if d.get("type") == "proposal_withheld"][:50],
        },
        "collaboration": _collaboration(state_dir),
        "performance": _performance(store, gw, decisions),
        "alerts": _alerts(runner, market, gw, agents, pf),
        "logs": _tail_lines(state_dir / "runner.log", 150),
        "integrations": [{k: i[k] for k in ("name", "installed", "used_by")} for i in all_status()],
        "verification": {"passed": verification["passed"], "counts": verification["counts"]}
        if verification
        else None,
    }


def agent_detail(state_dir: Path, agent_id: str) -> dict[str, Any] | None:
    if agent_id not in BY_ID:
        return None
    store = StateStore(state_dir)
    spec = BY_ID[agent_id]
    report = store.load_report(agent_id)
    return {
        "state": store.agents[agent_id],
        "report": report.to_dict() if report else None,
        "spec": {
            "mission": spec.mission,
            "responsibilities": spec.responsibilities,
            "inputs": spec.inputs,
            "outputs": spec.outputs,
            "tools": spec.tools,
            "access": spec.access,
            "integrations": spec.integrations,
            "gap": spec.gap,
        },
    }


def make_handler(state_dir: Path, gw: GatewayClient, html: bytes, stream_interval: float = STREAM_INTERVAL_S):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data: Any, code: int = 200) -> None:
            self._send(code, json.dumps(data, default=str).encode(), "application/json")

        def do_GET(self) -> None:
            url = urlparse(self.path)
            if url.path in ("/", "/index.html"):
                return self._send(200, html, "text/html; charset=utf-8")
            if url.path == "/api/state":
                return self._json(collect(state_dir, gw))
            if url.path.startswith("/api/agent/"):
                d = agent_detail(state_dir, url.path.rsplit("/", 1)[-1])
                return self._json(d) if d else self._json({"error": "unknown agent"}, 404)
            if url.path == "/api/logs":
                n = min(int(parse_qs(url.query).get("n", ["200"])[0]), 2000)
                return self._json({"lines": _tail_lines(state_dir / "runner.log", n)})
            if url.path == "/api/stream":
                return self._stream()
            self._json({"error": "not found"}, 404)

        def _stream(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            try:
                while True:
                    payload = json.dumps(collect(state_dir, gw), default=str)
                    self.wfile.write(f"event: state\ndata: {payload}\n\n".encode())
                    self.wfile.flush()
                    time.sleep(stream_interval)
            except (BrokenPipeError, ConnectionResetError):
                return

    return Handler


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8800)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--gateway", default="http://127.0.0.1:8790")
    ap.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    a = ap.parse_args()
    a.state_dir.mkdir(parents=True, exist_ok=True)
    handler = make_handler(a.state_dir, GatewayClient(a.gateway, timeout=5), HTML_FILE.read_bytes())
    srv = ThreadingHTTPServer((a.host, a.port), handler)
    srv.daemon_threads = True
    print(f"dashboard on http://{a.host}:{a.port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()


# Keep the spec list importable for tests that check every agent is represented.
__all__ = ["SPECS", "agent_detail", "collect", "make_handler"]
