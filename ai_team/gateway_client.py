"""
HTTP client for the independent risk gateway.

The coordinator can only *submit* proposals and *read* gateway state. There is
no method to change limits, and if the gateway is unreachable every proposal is
treated as rejected (fail closed).
"""

from __future__ import annotations

import json
import urllib.request


class GatewayClient:
    def __init__(self, url: str = "http://127.0.0.1:8790", timeout: float = 20.0) -> None:
        self.url = url.rstrip("/")
        self.timeout = timeout
        # Local service: never route through an outbound proxy.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _call(self, method: str, path: str, body: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.url + path, data=data, method=method, headers={"Content-Type": "application/json"}
        )
        with self._opener.open(req, timeout=self.timeout) as r:
            return json.loads(r.read())

    def health(self) -> dict:
        try:
            return self._call("GET", "/health")
        except OSError as e:
            return {"ok": False, "error": str(e)}

    def portfolio(self) -> dict:
        try:
            return self._call("GET", "/portfolio")
        except OSError:
            return {}

    def limits(self) -> dict:
        return self._call("GET", "/limits")

    def submit(self, proposal: dict) -> dict:
        try:
            return self._call("POST", "/proposals", proposal)
        except OSError as e:
            return {
                "approved": False,
                "reasons": [f"gateway unreachable, fail closed: {e}"],
                "proposal_id": proposal.get("proposal_id"),
            }
