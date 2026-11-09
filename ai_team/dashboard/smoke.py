"""
Smoke test for a running dashboard + risk gateway (used by CI).

  python -m ai_team.dashboard.smoke --dashboard http://127.0.0.1:8800 --gateway http://127.0.0.1:8790

Services start concurrently, so wait (with a deadline) until the gateway is
healthy and the dashboard reports it, instead of asserting on the first reply.
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.request


OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def get_json(url: str):
    with OPENER.open(url, timeout=5) as r:
        return json.load(r)


def check(ok: bool, message: str) -> None:
    # Explicit check instead of assert, which python -O would strip.
    if not ok:
        raise SystemExit(f"smoke test failed: {message}")


def wait_for(fn, deadline: float, what: str):
    last = None
    while time.monotonic() < deadline:
        try:
            result = fn()
            if result:
                return result
        except OSError as e:
            last = e
        time.sleep(0.5)
    raise SystemExit(f"timed out waiting for {what} (last error: {last})")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dashboard", default="http://127.0.0.1:8800")
    ap.add_argument("--gateway", default="http://127.0.0.1:8790")
    ap.add_argument("--timeout", type=float, default=60)
    a = ap.parse_args()
    deadline = time.monotonic() + a.timeout

    health = wait_for(lambda: get_json(a.gateway + "/health"), deadline, "risk gateway")
    check(
        health.get("mode") == "paper" and health.get("live_trading") is False,
        f"gateway not paper-only: {health}",
    )

    def dashboard_sees_gateway():
        s = get_json(a.dashboard + "/api/state")
        return s if s["gateway"]["health"].get("ok") else None

    state = wait_for(dashboard_sees_gateway, deadline, "dashboard to report the gateway")
    check(len(state["agents"]) == 50, f"expected 50 agents, got {len(state['agents'])}")
    check({x["id"] for x in state["agents"]} >= {"technical-analyst", "volume-analyst"}, "agent ids missing")
    gw = state["gateway"]["health"]
    check(gw.get("mode") == "paper" and gw.get("live_trading") is False, f"dashboard gateway view: {gw}")
    with OPENER.open(a.dashboard + "/", timeout=5) as r:
        page = r.read().decode()
        check("AI Trading Desk" in page and 'id="tab-agents"' in page, "dashboard page did not render")
    print(f"dashboard OK: {len(state['agents'])} agents, gateway {gw['mode']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
