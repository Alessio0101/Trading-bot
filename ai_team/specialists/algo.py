"""Algorithmic and Automated Trading specialists (43-50)."""

from __future__ import annotations

import ast
import json
from collections import defaultdict
from itertools import product
from pathlib import Path

import numpy as np
import pandas as pd

from .. import indicators as ind
from ..runtime import AgentContext, HandlerResult, NeedsIntegration
from .quant import backtest


REPO = Path(__file__).resolve().parents[2]
STRATEGY_DIRS = [REPO / "easy-start" / "user_data" / "strategies", REPO / "user_data" / "strategies"]


def algorithmic_trading_engineer(ctx: AgentContext) -> HandlerResult:
    inventory = []
    for d in STRATEGY_DIRS:
        for f in sorted(d.glob("*.py")) if d.exists() else []:
            tree = ast.parse(f.read_text(encoding="utf-8"))
            classes = [
                n
                for n in tree.body
                if isinstance(n, ast.ClassDef) and any(getattr(b, "id", "") == "IStrategy" for b in n.bases)
            ]
            for c in classes:
                methods = {n.name for n in c.body if isinstance(n, ast.FunctionDef)}
                required = {"populate_indicators", "populate_entry_trend", "populate_exit_trend"}
                inventory.append(
                    {
                        "file": str(f.relative_to(REPO)),
                        "class": c.name,
                        "valid_interface": required <= methods,
                        "missing": sorted(required - methods),
                    }
                )
    ok = bool(inventory) and all(i["valid_interface"] for i in inventory)
    return HandlerResult(
        f"{len(inventory)} freqtrade strategies found, all valid={ok}",
        {"strategies": inventory, "all_valid": ok},
    )


def strategy_deployment_engineer(ctx: AgentContext) -> HandlerResult:
    template = REPO / "easy-start" / "tools" / "bot_settings.template.json"
    cfg = json.loads(template.read_text(encoding="utf-8"))
    compose = (REPO / "easy-start" / "docker-compose.yml").read_text(encoding="utf-8")
    checks = {
        "template_dry_run_true": cfg.get("dry_run") is True,
        "no_keys_in_template": not cfg["exchange"].get("key") and not cfg["exchange"].get("secret"),
        "ui_bound_to_localhost_by_default": "${BIND_IP:-127.0.0.1}" in compose,
        "strategy_configured": "--strategy StrategieUsoara" in compose,
    }
    return HandlerResult(
        f"Deployment checks {sum(checks.values())}/{len(checks)} passed",
        {"checks": checks, "ready_for_paper": all(checks.values())},
    )


def signal_aggregation_specialist(ctx: AgentContext) -> HandlerResult:
    regime = ctx.upstream("market-regime-analyst")
    trending = regime.metrics["trending"] if regime and regime.status == "ok" else None
    flow = ctx.upstream("order-flow-analyst")
    flow_bias = flow.metrics["flow_bias"] if flow and flow.status == "ok" else 0.0
    ta = ctx.upstream("technical-analyst")
    ta_bias = ta.metrics["bias"] if ta and ta.status == "ok" else 0.0
    trend_family = {"trend_following", "momentum", "breakout", "swing"}

    by_pair: dict[tuple, list] = defaultdict(list)
    for rep in ctx.inbox.values():
        for s in rep.signals:
            by_pair[(s.pair, s.side)].append(s)

    proposals = []
    for (pair, side), sigs in by_pair.items():
        weights = []
        for s in sigs:
            w = s.confidence
            if trending is not None and s.strategy:
                fits = (s.strategy in trend_family) == trending
                w *= 1.15 if fits else 0.8
            weights.append(w)
        score = float(np.mean(weights)) + 0.05 * flow_bias + 0.05 * ta_bias
        if side == "long" and (pair, "exit") in by_pair:
            score -= 0.1  # conflicting views lower conviction
        best = sigs[int(np.argmax(weights))]
        proposals.append(
            {
                "pair": pair,
                "side": side,
                "confidence": round(min(max(score, 0), 0.95), 3),
                "entry_price": best.entry_price,
                "stop_loss": best.stop_loss,
                "take_profit": best.take_profit,
                "strategy": best.strategy,
                "supporting_agents": sorted({s.source for s in sigs}),
                "votes": len(sigs),
            }
        )
    proposals.sort(key=lambda p: p["confidence"], reverse=True)
    return HandlerResult(
        f"{len(proposals)} consensus proposal(s) from {sum(len(v) for v in by_pair.values())} signals",
        {
            "proposals": proposals,
            "regime_trending": trending,
            "flow_bias": flow_bias,
            "technical_bias": ta_bias,
        },
    )


def parameter_optimisation_specialist(ctx: AgentContext) -> HandlerResult:
    df = ctx.ohlcv(limit=720)
    split = int(len(df) * 0.7)
    ins, oos = df.iloc[:split], df.iloc[split - 200 :]  # warm-up overlap for indicators

    def rule(d: pd.DataFrame, fast: int, slow: int) -> pd.Series:
        return (ind.ema(d["close"], fast) > ind.ema(d["close"], slow)).astype(float)

    grid = []
    for fast, slow in product((8, 12, 20), (30, 50, 80)):
        m = backtest(ins, rule(ins, fast, slow), ctx.task.timeframe)
        grid.append({"fast": fast, "slow": slow, "is_sharpe": m["sharpe"]})
    best = max(grid, key=lambda g: g["is_sharpe"])
    oos_pos = rule(oos, best["fast"], best["slow"]).iloc[200:]
    oos_m = backtest(oos.iloc[200:], oos_pos, ctx.task.timeframe)
    gap = best["is_sharpe"] - oos_m["sharpe"]
    return HandlerResult(
        f"Best EMA {best['fast']}/{best['slow']}: IS Sharpe {best['is_sharpe']:.2f}, "
        f"OOS {oos_m['sharpe']:.2f} (gap {gap:.2f})",
        {"best": best, "oos": oos_m, "overfit_gap": gap, "grid": grid, "overfit_warning": gap > 1.0},
    )


def model_monitoring_specialist(ctx: AgentContext) -> HandlerResult:
    df = ctx.ohlcv(limit=720)
    r = df["close"].pct_change().dropna()
    ref, rec = r.iloc[:-168], r.iloc[-168:]

    def ks(a, b) -> float:
        grid = np.sort(np.concatenate([a, b]))
        ca = np.searchsorted(np.sort(a), grid, side="right") / len(a)
        cb = np.searchsorted(np.sort(b), grid, side="right") / len(b)
        return float(np.max(np.abs(ca - cb)))

    d_ret = ks(ref.to_numpy(), rec.to_numpy())
    v = df["volume"]
    d_vol = ks(v.iloc[:-168].to_numpy(), v.iloc[-168:].to_numpy())
    crit = 1.36 * np.sqrt((len(ref) + len(rec)) / (len(ref) * len(rec)))
    drift = d_ret > crit or d_vol > crit
    return HandlerResult(
        f"KS returns {d_ret:.3f}, volume {d_vol:.3f} (crit {crit:.3f}); drift={drift}",
        {"ks_returns": d_ret, "ks_volume": d_vol, "critical": float(crit), "drift": bool(drift)},
    )


def performance_attribution_analyst(ctx: AgentContext) -> HandlerResult:
    by_strategy: dict[str, float] = defaultdict(float)
    by_agent: dict[str, float] = defaultdict(float)
    for r in ctx.portfolio.get("realised", []):
        by_strategy[r.get("strategy") or "unknown"] += r["pnl"]
        for a in r.get("supporting_agents", []):
            by_agent[a] += r["pnl"]
    unrealised = ctx.portfolio.get("unrealised", {})
    for u in unrealised.values():
        by_strategy[u.get("strategy") or "unknown"] += u["pnl"]
    total = sum(by_strategy.values())
    return HandlerResult(
        f"Attributed {total:+.2f} P&L across {len(by_strategy)} strategies",
        {"by_strategy": dict(by_strategy), "by_agent": dict(by_agent), "total": total},
    )


def system_health_monitor(ctx: AgentContext) -> HandlerResult:
    df = ctx.ohlcv(limit=5)
    age_h = (pd.Timestamp.now(tz="UTC") - df.index[-1]).total_seconds() / 3600
    agent_states = ctx.task.params.get("agent_states", {})
    failing = sorted(a for a, s in agent_states.items() if s.get("status") == "error")
    checks = {
        "gateway_ok": bool(ctx.gateway_health.get("ok")),
        "data_fresh": ctx.data.name == "synthetic" or age_h < 3,
        "no_failing_agents": not failing,
    }
    return HandlerResult(
        f"Health {'OK' if all(checks.values()) else 'DEGRADED'}: {checks}",
        {
            "checks": checks,
            "failing_agents": failing,
            "data_source": ctx.data.name,
            "last_candle_age_h": round(age_h, 2),
        },
    )


def reinforcement_learning_researcher(ctx: AgentContext) -> HandlerResult:
    raise NeedsIntegration(
        "No RL framework/simulator installed (Qlib RL, RD-Agent, NautilusTrader or freqtrade freqai-rl)."
    )
