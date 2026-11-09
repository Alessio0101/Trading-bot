"""
Read-only dashboard for the AI trading team.

  python -m ai_team.dashboard.server --port 8800 [--gateway http://127.0.0.1:8790]

Shows all 50 specialists (status, current task, latest analysis, signals,
errors, performance, risk approvals), the gateway, the paper portfolio and the
decision log. It only reads state; it cannot submit proposals.
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ..gateway_client import GatewayClient
from ..integrations import all_status
from ..specs import CATEGORIES, WORKFLOWS
from ..state import DEFAULT_STATE_DIR, StateStore


HTML = (Path(__file__).parent / "index.html").read_bytes()


def collect(state_dir: Path, gateway: GatewayClient) -> dict:
    store = StateStore(state_dir)
    verification = state_dir / "verification.json"
    v = json.loads(verification.read_text()) if verification.exists() else None
    pf = gateway.portfolio()
    return {
        "agents": sorted(store.agents.values(), key=lambda a: a["number"]),
        "categories": list(CATEGORIES.values()),
        "workflows": WORKFLOWS,
        "gateway": gateway.health(),
        "portfolio": {
            k: pf.get(k)
            for k in (
                "equity",
                "cash",
                "exposure",
                "positions",
                "unrealised",
                "daily_pnl_pct",
                "drawdown_pct",
                "realised",
            )
        }
        if pf
        else None,
        "gateway_decisions": [],
        "decisions": store.decisions(60)[::-1],
        "integrations": [{k: i[k] for k in ("name", "installed", "used_by")} for i in all_status()],
        "verification": {"passed": v["passed"], "counts": v["counts"]} if v else None,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8800)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--gateway", default="http://127.0.0.1:8790")
    ap.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    a = ap.parse_args()
    gw = GatewayClient(a.gateway, timeout=5)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:
            pass

        def do_GET(self) -> None:
            if self.path.startswith("/api/state"):
                body, ctype = json.dumps(collect(a.state_dir, gw), default=str).encode(), "application/json"
            elif self.path in ("/", "/index.html"):
                body, ctype = HTML, "text/html; charset=utf-8"
            else:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    print(f"dashboard on http://{a.host}:{a.port}", flush=True)
    ThreadingHTTPServer((a.host, a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
