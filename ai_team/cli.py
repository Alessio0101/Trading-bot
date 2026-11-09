"""
Command line entry point used by the Claude Code agents.

  python -m ai_team.cli agent <agent-id> [--pair BTC/USDT] [--timeframe 1h] [--param k=v]
  python -m ai_team.cli workflow <name> [--pair ...]
  python -m ai_team.cli cycle [--pairs BTC/USDT,ETH/USDT] [--langgraph] [--no-submit]
  python -m ai_team.cli status | integrations | verify | list

Specialists never submit to the gateway; only `cycle` (coordinator) does.
"""

from __future__ import annotations

import argparse
import json
import sys

from .bus import MessageBus
from .coordinator import Coordinator, build_langgraph
from .data import KrakenPublicData, SyntheticMarketData
from .gateway_client import GatewayClient
from .integrations import all_status
from .models import Task
from .runtime import AgentContext, run_specialist
from .specs import BY_ID, SPECS, WORKFLOWS
from .state import StateStore


def _data(kind: str):
    return KrakenPublicData() if kind == "kraken" else SyntheticMarketData()


def _params(items: list[str]) -> dict:
    out = {}
    for it in items or []:
        k, _, v = it.partition("=")
        try:
            out[k] = json.loads(v)
        except json.JSONDecodeError:
            out[k] = v
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ai_team")
    ap.add_argument("--data", choices=["kraken", "synthetic"], default="kraken")
    ap.add_argument("--gateway", default="http://127.0.0.1:8790")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("agent")
    a.add_argument("agent_id", choices=sorted(BY_ID))
    a.add_argument("--pair", default="BTC/USDT")
    a.add_argument("--timeframe", default="1h")
    a.add_argument("--param", action="append")
    a.add_argument(
        "--upstream",
        action="append",
        default=[],
        help="agent id whose latest stored report is handed to this agent",
    )
    w = sub.add_parser("workflow")
    w.add_argument("name", choices=sorted(WORKFLOWS))
    w.add_argument("--pair", default="BTC/USDT")
    w.add_argument("--timeframe", default="1h")
    w.add_argument("--param", action="append")
    c = sub.add_parser("cycle")
    c.add_argument("--pairs", default="BTC/USDT,ETH/USDT,SOL/USDT")
    c.add_argument("--timeframe", default="1h")
    c.add_argument("--headline", action="append")
    c.add_argument("--langgraph", action="store_true")
    c.add_argument("--no-submit", action="store_true")
    sub.add_parser("status")
    sub.add_parser("integrations")
    sub.add_parser("list")
    sub.add_parser("verify")
    args = ap.parse_args(argv)

    if args.cmd == "list":
        for s in SPECS:
            print(f"{s.number:2d} {s.id:36s} {s.category:20s} {s.status}")
        return 0
    if args.cmd == "integrations":
        print(json.dumps(all_status(), indent=2))
        return 0
    if args.cmd == "verify":
        from .verify import main as verify_main

        return verify_main()

    state = StateStore()
    gw = GatewayClient(args.gateway)
    if args.cmd == "status":
        print(
            json.dumps(
                {
                    "gateway": gw.health(),
                    "agents": {
                        k: {x: v[x] for x in ("status", "online", "latest_analysis")}
                        for k, v in state.agents.items()
                    },
                },
                indent=2,
                default=str,
            )
        )
        return 0

    data = _data(args.data)
    if args.cmd == "agent":
        # Single specialist run: analysis only, never touches the gateway's write path.
        task = Task(args.agent_id, "ad-hoc analysis", args.pair, args.timeframe, _params(args.param))
        inbox = {}
        for up in args.upstream:
            if up not in BY_ID:
                ap.error(f"unknown upstream agent {up}")
            r = state.load_report(up)
            if r is None:
                ap.error(f"no stored report for upstream agent {up}; run it first")
            if r.metrics.get("pair") not in (None, args.pair):
                ap.error(f"upstream {up} report is for {r.metrics.get('pair')}, not {args.pair}")
            inbox[up] = r
        state.start(args.agent_id, task.objective)
        rep = run_specialist(
            args.agent_id,
            AgentContext(
                task,
                data,
                inbox,
                portfolio=gw.portfolio(),
                gateway_health=gw.health(),
                decisions=state.decisions(),
            ),
        )
        state.finish(rep)
        print(json.dumps(rep.to_dict(), indent=2, default=str))
        return 0 if rep.status != "error" else 1

    coord = Coordinator(data, gw, state, MessageBus(state.messages_file))
    if args.cmd == "workflow":
        out = coord.run_workflow(args.name, args.pair, args.timeframe, _params(args.param))
        print(json.dumps({k: v.to_dict() for k, v in out.items()}, indent=2, default=str))
        return 0
    if args.cmd == "cycle":
        pairs = args.pairs.split(",")
        if args.langgraph:
            graph = build_langgraph(coord)
            result = graph.invoke(
                {
                    "pairs": pairs,
                    "timeframe": args.timeframe,
                    "headlines": args.headline or [],
                    "submit": not args.no_submit,
                }
            )
            summary = result["summary"]
        else:
            summary = coord.cycle(pairs, args.timeframe, args.headline, not args.no_submit)
        print(json.dumps(summary, indent=2, default=str))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
