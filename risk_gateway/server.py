"""
HTTP front-end of the risk gateway.

Routes: GET /health, /limits, /portfolio, /decisions; POST /proposals.
There is deliberately no route that modifies limits or places live orders.
Run: python -m risk_gateway.server --port 8790 --data kraken|synthetic
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .gateway import HERE, PriceSource, RiskGateway


MAX_BODY = 64 * 1024


def make_handler(gw: RiskGateway):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:  # keep stdout clean
            pass

        def _send(self, code: int, body) -> None:
            data = json.dumps(body, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            if self.path == "/health":
                return self._send(200, gw.health())
            if self.path == "/limits":
                return self._send(200, {**gw.limits, "sha256": gw.limits_sha256, "read_only": True})
            if self.path == "/portfolio":
                return self._send(200, gw.snapshot())
            if self.path == "/decisions":
                lines = gw.log_file.read_text().splitlines()[-200:] if gw.log_file.exists() else []
                return self._send(200, [json.loads(x) for x in lines])
            self._send(404, {"error": "not found"})

        def do_POST(self) -> None:
            if self.path != "/proposals":
                return self._send(405, {"error": "only POST /proposals is accepted"})
            length = int(self.headers.get("Content-Length", 0))
            if length > MAX_BODY:
                return self._send(413, {"error": "body too large"})
            try:
                proposal = json.loads(self.rfile.read(length))
                if not isinstance(proposal, dict):
                    raise ValueError("proposal must be an object")
            except ValueError as e:
                return self._send(400, {"error": f"invalid JSON: {e}"})
            self._send(200, gw.evaluate(proposal))

        def do_PUT(self) -> None:
            self._send(405, {"error": "limits are read-only"})

        do_DELETE = do_PATCH = do_PUT

    return Handler


def serve(port: int, data: str, state_dir: Path) -> ThreadingHTTPServer:
    gw = RiskGateway(state_dir, PriceSource(data))
    return ThreadingHTTPServer(("127.0.0.1", port), make_handler(gw))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8790)
    ap.add_argument("--data", choices=["kraken", "synthetic"], default="kraken")
    ap.add_argument("--state-dir", type=Path, default=HERE / "state")
    a = ap.parse_args()
    srv = serve(a.port, a.data, a.state_dir)
    print(f"risk gateway (paper only) on http://127.0.0.1:{a.port} data={a.data}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
