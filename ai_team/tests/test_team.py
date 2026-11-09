import json

import pytest

from ai_team.bus import MessageBus
from ai_team.coordinator import Coordinator
from ai_team.gateway_client import GatewayClient
from ai_team.specs import SPECS, WORKFLOWS
from ai_team.state import StateStore
from ai_team.verify import check_files, check_settings


def test_agent_files_and_permissions():
    r = check_files()
    assert r["count_present"] == 51
    bad = {k: v["problems"] for k, v in r["files"].items() if not v["ok"]}
    assert not bad, bad
    assert check_settings()["ok"]


def test_spec_counts_per_category():
    from collections import Counter

    assert Counter(s.category for s in SPECS) == {
        "market_analysis": 10,
        "quant_research": 8,
        "trading_strategies": 10,
        "execution": 7,
        "risk": 7,
        "algo_automation": 8,
    }
    assert len({s.id for s in SPECS}) == 50


@pytest.mark.parametrize("spec", SPECS, ids=lambda s: f"{s.number:02d}-{s.id}")
def test_specialist_observed_matches_declared(verified_cycle, spec):
    a = verified_cycle["agents"][spec.id]
    assert a["runs"] > 0, "coordinator must invoke every specialist"
    assert a["observed"] != "error", a["errors"]
    assert a["consistent"], f"declared {a['declared']} but observed {a['observed']}"


@pytest.mark.parametrize("name", sorted(WORKFLOWS))
def test_workflow_handoffs(verified_cycle, name):
    w = verified_cycle["workflows"][name]
    assert w["observed"], f"hand-offs missing: {w}"


def test_cycle_submits_only_through_gateway(verified_cycle):
    assert verified_cycle["gateway_submissions"] >= 1
    assert verified_cycle["bypass_attempt"]["approved"] is False


def test_cycle_fails_closed_without_gateway(tmp_path, market):
    state = StateStore(tmp_path / "state")
    coord = Coordinator(
        market, GatewayClient("http://127.0.0.1:9", timeout=1), state, MessageBus(state.messages_file)
    )
    coord.cycle(["BTC/USDT"], "1h")
    decisions = state.decisions()
    subs = [d for d in decisions if d["type"] == "gateway_submission"]
    assert all(not d["gateway"]["approved"] for d in subs)
    withheld = [d for d in decisions if d["type"] == "proposal_withheld"]
    assert subs or withheld or any(d["type"] == "no_trade" for d in decisions)
    assert any(d["type"] == "cycle_start" for d in decisions)


def test_langgraph_graph_runs(tmp_path, market, gateway_url):
    pytest.importorskip("langgraph")
    from ai_team.coordinator import build_langgraph

    state = StateStore(tmp_path / "state")
    coord = Coordinator(market, GatewayClient(gateway_url), state)
    out = build_langgraph(coord).invoke({"pairs": ["BTC/USDT"], "timeframe": "1h", "submit": True})
    assert "BTC/USDT" in out["summary"]["pairs"]
    assert any(d["type"] == "langgraph_preflight" for d in state.decisions())


def test_langgraph_research_only_when_gateway_down(tmp_path, market):
    pytest.importorskip("langgraph")
    from ai_team.coordinator import build_langgraph

    state = StateStore(tmp_path / "state")
    coord = Coordinator(market, GatewayClient("http://127.0.0.1:9", timeout=1), state)
    build_langgraph(coord).invoke({"pairs": ["BTC/USDT"], "timeframe": "1h", "submit": True})
    assert not [d for d in state.decisions() if d["type"] == "gateway_submission"]


def test_decision_log_is_jsonl(verified_cycle):
    import pathlib

    path = pathlib.Path(verified_cycle["tmp_dir"]) / "state" / "decisions.jsonl"
    entries = [json.loads(x) for x in path.read_text().splitlines()]
    types = {e["type"] for e in entries}
    assert {"cycle_start", "compare_findings", "strategy_review", "cycle_end"} <= types


def test_cli_upstream_handoff(tmp_path, monkeypatch, capsys):
    """Separate CLI runs (as Claude Code subagents do) must pass numbers through code."""
    from ai_team import cli
    from ai_team import state as state_mod

    monkeypatch.setattr(state_mod, "DEFAULT_STATE_DIR", tmp_path)
    monkeypatch.setattr(state_mod.StateStore.__init__, "__defaults__", (tmp_path,))
    gw = ["--gateway", "http://127.0.0.1:9"]
    assert cli.main(["--data", "synthetic", *gw, "agent", "volume-analyst"]) == 0
    capsys.readouterr()
    assert (
        cli.main(["--data", "synthetic", *gw, "agent", "liquidity-analyst", "--upstream", "volume-analyst"])
        == 0
    )
    out = json.loads(capsys.readouterr().out)
    assert out["metrics"]["relative_volume"] is not None
    assert out["metrics"]["upstream_used"] == ["volume-analyst"]
    assert out["metrics"]["data_source"] == "synthetic"

    with pytest.raises(SystemExit):  # upstream for a different pair is refused
        cli.main(
            [
                "--data",
                "synthetic",
                *gw,
                "agent",
                "order-flow-analyst",
                "--pair",
                "ETH/USDT",
                "--upstream",
                "liquidity-analyst",
            ]
        )
    with pytest.raises(SystemExit):  # missing upstream report is refused
        cli.main(
            ["--data", "synthetic", *gw, "agent", "order-flow-analyst", "--upstream", "slippage-analyst"]
        )


def test_liquidity_flags_truncated_book(market):
    from ai_team.models import Task
    from ai_team.runtime import AgentContext, run_specialist

    class Shallow:
        name = "shallow"

        def order_book(self, pair, depth=500):
            return {"bids": [(99.99, 1.0), (99.98, 1.0)], "asks": [(100.01, 1.0), (100.02, 1.0)]}

        def __getattr__(self, item):
            return getattr(market, item)

    r = run_specialist("liquidity-analyst", AgentContext(Task("liquidity-analyst", "t"), Shallow()))
    assert r.status == "ok"
    assert r.metrics["depth_is_lower_bound"] == {"0.5pct": True, "1pct": True}


def test_backtest_charges_fee_and_slippage(market):
    import pandas as pd

    from ai_team.specialists.quant import FEE, SLIPPAGE, backtest

    df = market.ohlcv("BTC/USDT", "1h", 50).copy()
    df["close"] = 100.0  # flat market: any loss is pure trading cost
    pos = pd.Series([0, 1] * 25, index=df.index, dtype=float)
    r = backtest(df, pos, "1h")
    switches = pos.shift().fillna(0).diff().abs().fillna(0).sum()
    assert r["total_return"] == pytest.approx((1 - (FEE + SLIPPAGE)) ** switches - 1, rel=1e-9)


def _bt_report(**metrics):
    from ai_team.models import Report

    base = {
        "trades": 10,
        "sharpe": 1.5,
        "max_drawdown": -0.05,
        "total_return": 0.08,
        "out_of_sample": {"trades": 3, "sharpe": 0.9},
    }
    return Report("backtesting-specialist", "t", "ok", "", {"results": {"s": {**base, **metrics}}})


@pytest.mark.parametrize(
    ("metrics", "reason"),
    [
        ({}, None),
        ({"trades": 1}, "only 1 trades"),
        ({"sharpe": -0.2}, "Sharpe"),
        ({"max_drawdown": -0.4}, "drawdown"),
        ({"total_return": -0.01}, "net return"),
        ({"out_of_sample": {"trades": 0, "sharpe": 0.0}}, "no out-of-sample trades"),
        ({"out_of_sample": {"trades": 4, "sharpe": -0.5}}, "out-of-sample Sharpe"),
        ({"out_of_sample": None}, "no out-of-sample test"),
    ],
)
def test_independent_risk_reviewer_standards(market, metrics, reason):
    from ai_team.models import Task
    from ai_team.runtime import AgentContext, run_specialist

    rep = _bt_report(**metrics)
    if metrics.get("out_of_sample", 1) is None:
        del rep.metrics["results"]["s"]["out_of_sample"]
    ctx = AgentContext(Task("independent-risk-reviewer", "t"), market, {"backtesting-specialist": rep})
    v = run_specialist("independent-risk-reviewer", ctx).metrics["verdicts"]["s"]
    if reason is None:
        assert v["verdict"] == "APPROVE", v
    else:
        assert v["verdict"] == "REJECT"
        assert any(reason in r for r in v["reasons"]), v["reasons"]


def test_quantitative_researcher_ranks_by_evidence(market):
    from ai_team.models import Task
    from ai_team.runtime import AgentContext, run_specialist
    from ai_team.specialists.quant import STRATEGY_HYPOTHESIS

    m = run_specialist(
        "quantitative-researcher", AgentContext(Task("quantitative-researcher", "t"), market)
    ).metrics
    ics = [r["ic"] for r in m["ranking"]]
    assert ics == sorted(ics, reverse=True)
    assert set(m["candidate_strategies"]) == set(STRATEGY_HYPOTHESIS)
    assert m["supported_strategies"] == [r["strategy"] for r in m["ranking"] if r["ic"] > 0]


def test_dashboard_state_reports_unreachable_gateway(tmp_path):
    """While the gateway is down the dashboard must say so instead of inventing a mode."""
    from ai_team.dashboard.server import collect

    state = collect(tmp_path, GatewayClient("http://127.0.0.1:9", timeout=1))
    assert state["gateway"]["health"]["ok"] is False
    assert "mode" not in state["gateway"]["health"]
    assert len(state["agents"]) == 50
    assert state["gateway"]["portfolio"] is None
    assert state["runner"] is None and state["market"] is None
    levels = {a["level"] for a in state["alerts"]}
    texts = " ".join(a["text"] for a in state["alerts"])
    assert (
        "critical" in levels and "Risk gateway unreachable" in texts and "Runner has never started" in texts
    )


def test_dashboard_state_with_gateway(tmp_path, gateway_url):
    from ai_team.dashboard.server import collect

    state = collect(tmp_path, GatewayClient(gateway_url))
    h = state["gateway"]["health"]
    assert h["mode"] == "paper" and h["live_trading"] is False
    assert state["gateway"]["portfolio"]["equity"] == pytest.approx(10000.0)
    assert state["gateway"]["limits"]["read_only"] is True
    assert state["mode"] == {"paper": True, "live_trading": False}
