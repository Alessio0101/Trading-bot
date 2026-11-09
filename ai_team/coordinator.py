"""
Trading Coordinator.

Assigns tasks, runs independent work in parallel, passes reports along the
collaboration workflows, requests independent reviews and submits proposals to
the independent risk gateway. It records every decision. It has no way to
bypass the gateway: it holds no broker, no credentials and no limit controls.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from itertools import pairwise
from typing import Any, TypedDict

from .bus import MessageBus
from .data import MarketData
from .gateway_client import GatewayClient
from .models import Report, Task, new_id
from .runtime import DEFAULT_UNIVERSE, AgentContext, run_specialist
from .specs import COORDINATOR_ID, WORKFLOWS
from .state import StateStore


# Stage plan for one pair. Agents inside a stage are independent -> run in parallel.
STAGE_RESEARCH = [
    "data-engineer",
    "technical-analyst",
    "chart-pattern-analyst",
    "volume-analyst",
    "market-regime-analyst",
    "volatility-analyst",
    "correlation-analyst",
    "factor-researcher",
    "statistical-arbitrage-researcher",
    "quantitative-researcher",
    "market-microstructure-analyst",
    "macro-economic-analyst",
    "fundamental-analyst",
    "news-analyst",
    "model-monitoring-specialist",
]
STAGE_CHAINS = [  # each chain is sequential; chains run in parallel
    ("volume_liquidity_orderflow", ["liquidity-analyst", "order-flow-analyst"]),
    ("news_sentiment_event", ["sentiment-analyst", "event-driven-specialist"]),
    ("data_features_ml", ["feature-engineer", "machine-learning-researcher"]),
]
STAGE_STRATEGIES = [
    "trend-following-strategist",
    "mean-reversion-strategist",
    "momentum-strategist",
    "breakout-strategist",
    "swing-trading-strategist",
    "scalping-strategist",
    "arbitrage-strategist",
    "options-strategist",
    "pairs-trading-strategist",
]
UPSTREAM_OF = {  # recipient -> upstream senders (for workflow hand-off messages)
    "liquidity-analyst": ["volume-analyst"],
    "order-flow-analyst": ["liquidity-analyst"],
    "sentiment-analyst": ["news-analyst"],
    "event-driven-specialist": ["sentiment-analyst"],
    "feature-engineer": ["data-engineer"],
    "machine-learning-researcher": ["feature-engineer"],
    "trend-following-strategist": ["technical-analyst"],
    "swing-trading-strategist": ["technical-analyst"],
    "breakout-strategist": ["chart-pattern-analyst"],
    "pairs-trading-strategist": ["statistical-arbitrage-researcher"],
    "backtesting-specialist": ["quantitative-researcher"],
    "independent-risk-reviewer": ["backtesting-specialist"],
    "position-sizing-specialist": ["volatility-analyst"],
    "portfolio-risk-analyst": ["position-sizing-specialist", "correlation-analyst"],
    "slippage-analyst": ["liquidity-analyst", "execution-trader"],
    "transaction-cost-analyst": ["slippage-analyst"],
}
WORKFLOW_OF: dict[tuple[str, str], str] = {}
for _wf, _agents in WORKFLOWS.items():
    for _a, _b in pairwise(_agents):
        WORKFLOW_OF.setdefault((_a, _b), _wf)


class Coordinator:
    def __init__(
        self,
        data: MarketData,
        gateway: GatewayClient,
        state: StateStore,
        bus: MessageBus | None = None,
        max_workers: int = 8,
        universe: list[str] | None = None,
    ) -> None:
        self.data = data
        self.gateway = gateway
        self.state = state
        self.bus = bus or MessageBus(state.messages_file)
        self.max_workers = max_workers
        self.universe = universe or list(DEFAULT_UNIVERSE)

    # ---------------- primitives ----------------
    def assign(
        self,
        agent_id: str,
        objective: str,
        pair: str,
        timeframe: str,
        inbox: dict[str, Report],
        params: dict | None = None,
        portfolio: dict | None = None,
        health: dict | None = None,
    ) -> Report:
        task = Task(agent_id, objective, pair, timeframe, params or {})
        self.bus.publish(
            COORDINATOR_ID, agent_id, "task", {"task_id": task.task_id, "objective": objective, "pair": pair}
        )
        for up in UPSTREAM_OF.get(agent_id, []):
            if up in inbox:
                self.bus.publish(
                    up,
                    agent_id,
                    "handoff",
                    {"summary": inbox[up].summary, "status": inbox[up].status},
                    workflow=WORKFLOW_OF.get((up, agent_id)),
                )
        self.state.start(agent_id, f"{objective} [{pair} {timeframe}]")
        ctx = AgentContext(
            task, self.data, dict(inbox), self.universe, portfolio or {}, health or {}, self.state.decisions()
        )
        report = run_specialist(agent_id, ctx)
        self.state.finish(report)
        self.bus.publish(
            agent_id,
            COORDINATOR_ID,
            "report",
            {"status": report.status, "summary": report.summary, "signals": len(report.signals)},
        )
        return report

    def run_parallel(
        self, agent_ids: list[str], objective: str, pair: str, timeframe: str, inbox: dict[str, Report], **kw
    ) -> dict[str, Report]:
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futs = {
                a: pool.submit(self.assign, a, objective, pair, timeframe, inbox, **kw) for a in agent_ids
            }
            return {a: f.result() for a, f in futs.items()}

    def run_chain(
        self, agent_ids: list[str], objective: str, pair: str, timeframe: str, inbox: dict[str, Report], **kw
    ) -> dict[str, Report]:
        local = dict(inbox)
        out = {}
        for a in agent_ids:
            out[a] = local[a] = self.assign(a, objective, pair, timeframe, local, **kw)
        return out

    def run_workflow(
        self, name: str, pair: str = "BTC/USDT", timeframe: str = "1h", params: dict | None = None
    ) -> dict[str, Report]:
        agents = WORKFLOWS[name]
        self.state.record_decision("workflow_start", workflow=name, agents=agents, pair=pair)
        out = self.run_chain(
            agents,
            f"workflow {name}",
            pair,
            timeframe,
            {},
            params=params or {},
            portfolio=self.gateway.portfolio(),
            health=self.gateway.health(),
        )
        self.state.record_decision(
            "workflow_end", workflow=name, results={a: r.status for a, r in out.items()}
        )
        return out

    # ---------------- full cycle ----------------
    def cycle(
        self,
        pairs: list[str] | None = None,
        timeframe: str = "1h",
        headlines: list[str] | None = None,
        submit: bool = True,
    ) -> dict[str, Any]:
        cycle_id = new_id("cycle")
        health = self.gateway.health()
        portfolio = self.gateway.portfolio()
        self.state.record_decision(
            "cycle_start",
            cycle_id=cycle_id,
            pairs=pairs or self.universe,
            gateway=health,
            data_source=self.data.name,
        )
        summary: dict[str, Any] = {"cycle_id": cycle_id, "pairs": {}, "gateway": health}
        for pair in pairs or self.universe:
            summary["pairs"][pair] = self._cycle_pair(
                cycle_id, pair, timeframe, headlines or [], portfolio, health, submit
            )
            portfolio = self.gateway.portfolio()
        post = self._post_trade(cycle_id, timeframe, portfolio, health)
        summary["post_trade"] = {a: r.summary for a, r in post.items()}
        self.state.record_decision("cycle_end", cycle_id=cycle_id, summary=summary)
        return summary

    def _cycle_pair(self, cycle_id, pair, tf, headlines, portfolio, health, submit) -> dict:
        kw = {"portfolio": portfolio, "health": health}
        inbox: dict[str, Report] = {}
        params = {"headlines": headlines} if headlines else {}

        inbox |= self.run_parallel(STAGE_RESEARCH, "market research", pair, tf, inbox, params=params, **kw)
        with ThreadPoolExecutor(max_workers=len(STAGE_CHAINS)) as pool:
            futs = [
                pool.submit(self.run_chain, agents, f"workflow {wf}", pair, tf, inbox, params=params, **kw)
                for wf, agents in STAGE_CHAINS
            ]
            for f in futs:
                inbox |= f.result()
        inbox |= self.run_parallel(STAGE_STRATEGIES, "propose trades", pair, tf, inbox, **kw)
        self._compare(cycle_id, pair, inbox)

        agg = self.assign("signal-aggregation-specialist", "aggregate signals", pair, tf, inbox, **kw)
        inbox[agg.agent_id] = agg
        proposals = [p for p in agg.metrics.get("proposals", []) if p["pair"] == pair]

        # Independent strategy review: backtest + reviewer who did not propose.
        strategies = sorted({p["strategy"] for p in proposals if p.get("strategy")})
        qr = inbox.get("quantitative-researcher")
        cands = sorted(
            set(strategies)
            | set(qr.metrics.get("candidate_strategies", []) if qr and qr.status == "ok" else [])
        )
        bt = self.assign(
            "backtesting-specialist",
            "independent backtest",
            pair,
            tf,
            {"quantitative-researcher": _with_candidates(qr, cands)} if qr else {},
            **kw,
        )
        rev = self.assign(
            "independent-risk-reviewer",
            "independent strategy review",
            pair,
            tf,
            {"backtesting-specialist": bt},
            **kw,
        )
        inbox |= {bt.agent_id: bt, rev.agent_id: rev}
        self.state.record_decision(
            "strategy_review",
            cycle_id=cycle_id,
            pair=pair,
            backtest=bt.metrics.get("results"),
            verdicts=rev.metrics.get("verdicts"),
        )

        results = []
        for p in proposals:
            results.append(self._process_proposal(cycle_id, pair, tf, p, inbox, bt, rev, submit, **kw))
        if not proposals:
            self.state.record_decision(
                "no_trade", cycle_id=cycle_id, pair=pair, reason="no strategy produced a signal"
            )
        return {"proposals": results, "agents": {a: r.status for a, r in inbox.items()}}

    def _compare(self, cycle_id: str, pair: str, inbox: dict[str, Report]) -> None:
        views: dict[str, list[str]] = {}
        for a, r in inbox.items():
            for s in r.signals:
                views.setdefault(s.side, []).append(a)
        biases = {
            a: inbox[a].metrics.get(k)
            for a, k in (
                ("technical-analyst", "bias"),
                ("order-flow-analyst", "flow_bias"),
                ("market-regime-analyst", "regime"),
            )
            if a in inbox and inbox[a].status == "ok"
        }
        self.state.record_decision(
            "compare_findings",
            cycle_id=cycle_id,
            pair=pair,
            signal_views=views,
            biases=biases,
            conflict=len(views) > 1,
        )

    def _process_proposal(self, cycle_id, pair, tf, p, inbox, bt, rev, submit, portfolio, health) -> dict:
        kw = {"portfolio": portfolio, "health": health}
        pid = new_id("prop")
        strategy = p.get("strategy")
        verdict = rev.metrics.get("verdicts", {}).get(
            strategy, {"verdict": "REJECT", "reasons": ["not reviewed"]}
        )
        bt_m = bt.metrics.get("results", {}).get(strategy)
        reviews = [
            {
                "reviewer": "backtesting-specialist",
                "verdict": "APPROVE" if bt_m and bt_m["sharpe"] > 0 else "REJECT",
                "metrics": bt_m,
            },
            {
                "reviewer": "independent-risk-reviewer",
                "verdict": verdict["verdict"],
                "reasons": verdict["reasons"],
            },
        ]
        record: dict[str, Any] = {
            "proposal_id": pid,
            "pair": pair,
            "side": p["side"],
            "strategy": strategy,
            "confidence": p["confidence"],
            "supporting_agents": p["supporting_agents"],
            "reviews": reviews,
        }

        stake = None
        if p["side"] == "long":
            risk_inbox = {"volatility-analyst": inbox["volatility-analyst"]}
            ps = self.assign("position-sizing-specialist", "size proposal", pair, tf, risk_inbox, **kw)
            risk_inbox |= {ps.agent_id: ps, "correlation-analyst": inbox["correlation-analyst"]}
            pr = self.assign("portfolio-risk-analyst", "portfolio impact", pair, tf, risk_inbox, **kw)
            stake = ps.metrics.get("stake")
            exe_params = {"proposal": {**p, "stake": stake}}
            ex_inbox = {"liquidity-analyst": inbox["liquidity-analyst"]}
            et = self.assign(
                "execution-trader", "plan paper order", pair, tf, ex_inbox, params=exe_params, **kw
            )
            ex_inbox[et.agent_id] = et
            sl = self.assign("slippage-analyst", "estimate slippage", pair, tf, ex_inbox, **kw)
            tca = self.assign("transaction-cost-analyst", "cost vs edge", pair, tf, {sl.agent_id: sl}, **kw)
            rt = self.assign("order-routing-specialist", "route order", pair, tf, {}, **kw)
            st = self.assign("stress-testing-specialist", "stress portfolio", pair, tf, {}, **kw)
            record.update(
                stake=stake,
                portfolio_risk=pr.metrics,
                order_plan=et.metrics.get("order_plan"),
                cost=tca.metrics,
                routing=rt.metrics,
                stress=st.metrics.get("scenarios"),
            )
            blocking = []
            if any(r["verdict"] != "APPROVE" for r in reviews):
                blocking.append("independent reviews did not approve the strategy")
            if not tca.metrics.get("acceptable", False):
                blocking.append("transaction costs too high relative to edge")
            if not pr.metrics.get("advisory_ok", False):
                blocking.append("portfolio risk analyst advises against")
        else:
            blocking = [] if pair in portfolio.get("positions", {}) else ["no open paper position to exit"]

        comp = self.assign(
            "compliance-officer",
            "pre-submission compliance",
            pair,
            tf,
            {},
            params={"proposals": [record]},
            **kw,
        )
        if not comp.metrics.get("passed"):
            blocking.append("compliance check failed")
        record["compliance"] = comp.metrics.get("checks")

        if blocking or not submit:
            record["coordinator_decision"] = "withheld"
            record["reasons"] = blocking or ["submission disabled for this run"]
            self.state.record_decision("proposal_withheld", cycle_id=cycle_id, **record)
            return record

        gw_proposal = {
            "proposal_id": pid,
            "pair": pair,
            "side": p["side"],
            "confidence": p["confidence"],
            "stake": stake,
            "entry_price": p.get("entry_price"),
            "stop_loss": p.get("stop_loss"),
            "take_profit": p.get("take_profit"),
            "strategy": strategy,
            "supporting_agents": p["supporting_agents"],
            "reviews": [{"reviewer": r["reviewer"], "verdict": r["verdict"]} for r in reviews],
            "rationale": f"consensus of {p['votes']} signal(s)",
            "submitted_by": COORDINATOR_ID,
        }
        decision = self.gateway.submit(gw_proposal)
        record["coordinator_decision"] = "submitted"
        record["gateway"] = decision
        self.state.risk_result(
            p["supporting_agents"] + ["signal-aggregation-specialist"],
            bool(decision.get("approved")),
            "; ".join(decision.get("reasons", [])),
        )
        self.state.record_decision("gateway_submission", cycle_id=cycle_id, **record)
        return record

    def _post_trade(self, cycle_id, tf, portfolio, health) -> dict[str, Report]:
        kw = {"portfolio": portfolio, "health": health}
        agents = [
            "trade-reconciliation-specialist",
            "drawdown-monitor",
            "performance-attribution-analyst",
            "algorithmic-trading-engineer",
            "strategy-deployment-engineer",
            "parameter-optimisation-specialist",
            "algorithmic-execution-specialist",
            "reinforcement-learning-researcher",
        ]
        out = self.run_parallel(agents, "post-trade review", self.universe[0], tf, {}, **kw)
        states = {a: {"status": s["status"]} for a, s in self.state.agents.items()}
        out["system-health-monitor"] = self.assign(
            "system-health-monitor",
            "health check",
            self.universe[0],
            tf,
            {},
            params={"agent_states": states},
            **kw,
        )
        self.state.record_decision(
            "post_trade", cycle_id=cycle_id, results={a: r.summary for a, r in out.items()}
        )
        return out


def _with_candidates(report: Report, candidates: list[str]) -> Report:
    clone = Report(**{**report.__dict__, "metrics": {**report.metrics, "candidate_strategies": candidates}})
    return clone


class CycleState(TypedDict, total=False):
    pairs: list[str]
    timeframe: str
    headlines: list[str]
    submit: bool
    health: dict
    summary: dict


def build_langgraph(coordinator: Coordinator):
    """
    Express the coordinator cycle as a LangGraph StateGraph when LangGraph is
    installed. The nodes reuse the same Coordinator methods, so behaviour is identical.
    """
    from langgraph.graph import END, START, StateGraph

    def preflight(s: CycleState) -> CycleState:
        h = coordinator.gateway.health()
        coordinator.state.record_decision("langgraph_preflight", gateway=h)
        return {"health": h}

    def route(s: CycleState) -> str:
        # Fail closed: without a healthy paper-only gateway nothing is submitted.
        ok = s["health"].get("ok") and s["health"].get("mode") == "paper"
        return "cycle" if ok else "research_only"

    def cycle(s: CycleState) -> CycleState:
        return {
            "summary": coordinator.cycle(
                s.get("pairs"), s.get("timeframe", "1h"), s.get("headlines"), s.get("submit", True)
            )
        }

    def research_only(s: CycleState) -> CycleState:
        return {
            "summary": coordinator.cycle(
                s.get("pairs"), s.get("timeframe", "1h"), s.get("headlines"), submit=False
            )
        }

    g = StateGraph(CycleState)
    g.add_node("preflight", preflight)
    g.add_node("cycle", cycle)
    g.add_node("research_only", research_only)
    g.add_edge(START, "preflight")
    g.add_conditional_edges("preflight", route, {"cycle": "cycle", "research_only": "research_only"})
    g.add_edge("cycle", END)
    g.add_edge("research_only", END)
    return g.compile()
