"""
Verification of the AI trading team (python -m ai_team.cli verify).

1. All 51 agent files exist and match the specs.
2. Frontmatter, required sections and tool permissions are correct.
3. Project settings deny edits to the gateway and reads of credentials.
4. A real coordinator cycle (synthetic data, temporary gateway) invokes every
   specialist; each agent's *observed* result is compared with its declared
   capability, so a Markdown file alone never counts as a working agent.
5. Workflow hand-offs are present on the message bus.
Writes ai_team/state/verification.json and VERIFICATION.md.
"""

from __future__ import annotations

import json
import re
import tempfile
import threading
from itertools import pairwise
from pathlib import Path

from .bus import MessageBus
from .coordinator import Coordinator
from .data import SyntheticMarketData
from .gateway_client import GatewayClient
from .generate_agents import AGENTS_DIR, REPO
from .integrations import all_status
from .specs import COORDINATOR_ID, SPECS, WORKFLOWS
from .state import DEFAULT_STATE_DIR, StateStore


REQUIRED_SECTIONS = [
    "## Responsibilities",
    "## Inputs",
    "## Outputs",
    "## Tools and permissions",
    "## Integrations",
    "## GitHub",
    "## Collaboration",
    "## Testing and reporting",
    "## Non-negotiable safety rules",
]
FORBIDDEN_FOR_READ_ONLY = {"Edit", "Write", "NotebookEdit", "Agent"}
FORBIDDEN_EVERYWHERE = {
    "mcp__github__merge_pull_request",
    "mcp__github__push_files",
    "mcp__github__delete_file",
}
HEADLINES = [
    "Major exchange announces listing and partnership, analysts see rally",
    "Regulator approval boosts inflow into crypto funds",
]


def parse_frontmatter(text: str) -> dict:
    m = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    if not m:
        return {}
    out = {}
    for line in m.group(1).splitlines():
        k, _, v = line.partition(":")
        out[k.strip()] = v.strip()
    return out


def check_files() -> dict:
    results = {}
    expected = {f"{s.number:02d}-{s.id}.md": s for s in SPECS}
    expected[f"00-{COORDINATOR_ID}.md"] = None
    present = {p.name for p in AGENTS_DIR.glob("*.md")}
    names = []
    for fname, spec in expected.items():
        r = {"exists": fname in present, "problems": []}
        if r["exists"]:
            text = (AGENTS_DIR / fname).read_text(encoding="utf-8")
            fm = parse_frontmatter(text)
            tools = {t.strip() for t in fm.get("tools", "").split(",") if t.strip()}
            names.append(fm.get("name"))
            agent_id = spec.id if spec else COORDINATOR_ID
            if fm.get("name") != agent_id:
                r["problems"].append(f"name {fm.get('name')!r} != {agent_id!r}")
            if not fm.get("description"):
                r["problems"].append("missing description")
            if tools & FORBIDDEN_EVERYWHERE:
                r["problems"].append(f"forbidden tools {sorted(tools & FORBIDDEN_EVERYWHERE)}")
            if spec:
                for sec in REQUIRED_SECTIONS:
                    if sec not in text:
                        r["problems"].append(f"missing section {sec}")
                if spec.access != "engineering" and tools & FORBIDDEN_FOR_READ_ONLY:
                    r["problems"].append(f"read-only agent has {sorted(tools & FORBIDDEN_FOR_READ_ONLY)}")
                if spec.access == "engineering" and "Agent" in tools:
                    r["problems"].append("engineering agent must not spawn agents")
                r["tools"] = sorted(tools)
                r["access"] = spec.access
            else:
                if "Agent" not in tools:
                    r["problems"].append("coordinator cannot invoke subagents (no Agent tool)")
                if {"Edit", "Write"} & tools:
                    r["problems"].append("coordinator must not edit files")
                for word in ("never bypass", "risk gateway"):
                    if word not in text.lower():
                        r["problems"].append(f"coordinator text lacks '{word}'")
        r["ok"] = r["exists"] and not r["problems"]
        results[fname] = r
    dupes = {n for n in names if names.count(n) > 1}
    return {
        "files": results,
        "count_present": len(present & set(expected)),
        "duplicate_names": sorted(dupes),
        "unexpected_files": sorted(present - set(expected)),
    }


def check_settings() -> dict:
    path = REPO / ".claude" / "settings.json"
    if not path.exists():
        return {"ok": False, "problems": ["missing .claude/settings.json"]}
    deny = json.loads(path.read_text())["permissions"]["deny"]
    # Edit(...) rules cover every file-editing tool (Edit, Write, NotebookEdit).
    needed = [
        "Edit(/risk_gateway/**)",
        "Edit(risk_gateway/**)",
        "Read(/easy-start/user_data/bot_settings.json)",
        "Read(**/.env)",
        "Edit(/.claude/settings.json)",
        "Bash(docker:*)",
        "Bash(curl:*)",
    ]
    missing = [n for n in needed if n not in deny]
    return {"ok": not missing, "missing_deny_rules": missing, "deny": deny}


def run_live_cycle() -> dict:
    """Invoke every specialist through the coordinator against a temporary paper gateway."""
    from risk_gateway.gateway import PriceSource, RiskGateway
    from risk_gateway.server import ThreadingHTTPServer, make_handler

    tmp = Path(tempfile.mkdtemp(prefix="ai_team_verify_"))
    data = SyntheticMarketData(seed=11, drift=0.0012)  # trending market so proposals exist
    gw = RiskGateway(tmp / "gateway", PriceSource("synthetic"))
    gw.prices._src = data
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(gw))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    state = StateStore(tmp / "state")
    bus = MessageBus(state.messages_file)
    coord = Coordinator(data, GatewayClient(url), state, bus)
    try:
        summary = coord.cycle(["BTC/USDT", "ETH/USDT", "SOL/USDT"], "1h", HEADLINES)
        wf_results = {
            name: {a: r.status for a, r in coord.run_workflow(name, params={"headlines": HEADLINES}).items()}
            for name in WORKFLOWS
        }
        bypass = GatewayClient(url).submit(
            {
                "proposal_id": "bypass-test",
                "pair": "BTC/USDT",
                "side": "long",
                "confidence": 0.99,
                "stake": 100,
                "strategy": "x",
                "reviews": [],
                "submitted_by": "technical-analyst",
                "override_limits": True,
            }
        )
    finally:
        srv.shutdown()

    agents = {}
    for spec in SPECS:
        a = state.agents[spec.id]
        perf = a["performance"]
        if perf["runs"] == 0:
            observed = "not_invoked"
        elif perf["errors"]:
            observed = "error"
        elif perf["ok"]:
            observed = "ok"
        else:
            observed = "needs_integration"
        expected = {
            "functional": {"ok"},
            "partial": {"ok", "needs_integration"},
            "needs_integration": {"needs_integration"},
        }[spec.status]
        agents[spec.id] = {
            "number": spec.number,
            "declared": spec.status,
            "observed": observed,
            "runs": perf["runs"],
            "consistent": observed in expected,
            "latest": a["latest_analysis"],
            "errors": a["errors"],
            "gap": spec.gap,
        }

    handoffs = {name: bus.chain(name) for name in WORKFLOWS}
    wf_check = {}
    for name, chain in WORKFLOWS.items():
        need = list(pairwise(chain))
        wf_check[name] = {
            "expected_handoffs": need,
            "observed": all(tuple(h) in handoffs[name] for h in need),
            "statuses": wf_results[name],
        }
    submissions = [d for d in state.decisions() if d["type"] == "gateway_submission"]
    return {
        "agents": agents,
        "workflows": wf_check,
        "cycle_pairs": list(summary["pairs"]),
        "gateway_submissions": len(submissions),
        "gateway_approved": sum(1 for d in submissions if d["gateway"].get("approved")),
        "bypass_attempt": bypass,
        "decisions_logged": len(state.decisions()),
        "messages": len(bus.messages),
        "tmp_dir": str(tmp),
    }


def main() -> int:
    files = check_files()
    settings = check_settings()
    live = run_live_cycle()
    integ = all_status()
    agents = live["agents"]
    counts = {
        k: sum(1 for a in agents.values() if a["observed"] == k)
        for k in ("ok", "needs_integration", "error", "not_invoked")
    }
    report = {"files": files, "settings": settings, "live": live, "integrations": integ, "counts": counts}
    all_ok = (
        files["count_present"] == 51
        and all(f["ok"] for f in files["files"].values())
        and not files["duplicate_names"]
        and settings["ok"]
        and all(a["consistent"] for a in agents.values())
        and counts["error"] == 0
        and counts["not_invoked"] == 0
        and all(w["observed"] for w in live["workflows"].values())
        and not live["bypass_attempt"].get("approved")
    )
    report["passed"] = all_ok
    DEFAULT_STATE_DIR.mkdir(parents=True, exist_ok=True)
    (DEFAULT_STATE_DIR / "verification.json").write_text(json.dumps(report, indent=1, default=str))
    (REPO / "ai_team" / "VERIFICATION.md").write_text(render_md(report), encoding="utf-8")
    print(render_md(report))
    return 0 if all_ok else 1


def render_md(r: dict) -> str:
    f, live = r["files"], r["live"]
    lines = [
        "# AI trading team - verification report",
        "",
        f"Overall: **{'PASS' if r['passed'] else 'FAIL'}**",
        "",
        "## 1. Agent files",
        f"- Present: {f['count_present']}/51 (50 specialists + coordinator)",
        f"- Definition/permission problems: {sum(1 for x in f['files'].values() if not x['ok'])}",
        f"- Duplicate names: {f['duplicate_names'] or 'none'}",
        f"- Settings deny rules complete: {r['settings']['ok']}",
    ]
    for name, x in f["files"].items():
        if not x["ok"]:
            lines.append(f"  - {name}: {x['problems']}")
    lines += [
        "",
        "## 2. Coordinator invocation (real run, synthetic data, temporary paper gateway)",
        f"- Specialists invoked: {50 - r['counts']['not_invoked']}/50",
        (
            f"- Observed: ok={r['counts']['ok']}, needs_integration={r['counts']['needs_integration']}, "
            f"error={r['counts']['error']}, not_invoked={r['counts']['not_invoked']}"
        ),
        f"- Gateway submissions: {live['gateway_submissions']} (approved {live['gateway_approved']})",
        (
            "- Bypass attempt (non-coordinator, no reviews, override field): "
            f"{'APPROVED - FAILURE' if live['bypass_attempt'].get('approved') else 'rejected'} "
            f"- {live['bypass_attempt'].get('reasons')}"
        ),
        f"- Decisions logged: {live['decisions_logged']}, bus messages: {live['messages']}",
        "",
        "## 3. Workflows",
    ]
    for name, w in live["workflows"].items():
        lines.append(f"- {name}: hand-offs {'OK' if w['observed'] else 'MISSING'}; statuses {w['statuses']}")
    lines += [
        "",
        "## 4. Per-agent result",
        "",
        "| # | Agent | Declared | Observed | Consistent | Latest output / gap |",
        "|---|-------|----------|----------|------------|---------------------|",
    ]
    for aid, a in sorted(live["agents"].items(), key=lambda kv: kv[1]["number"]):
        note = (a["latest"] or "")[:90].replace("|", "/")
        if a["observed"] != "ok" and a["gap"]:
            note = a["gap"][:90]
        lines.append(
            f"| {a['number']} | {aid} | {a['declared']} | {a['observed']} | "
            f"{'yes' if a['consistent'] else 'NO'} | {note} |"
        )
    lines += ["", "## 5. External integrations", ""]
    for i in r["integrations"]:
        lines.append(
            f"- {i['name']}: {'installed ' + str(i.get('version')) if i['installed'] else 'NOT installed'}"
            f" - used by {len(i['used_by'])} agent(s)"
        )
    return "\n".join(lines) + "\n"
