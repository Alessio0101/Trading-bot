"""
Risk Management specialists (36-42).

They *advise*. The binding decision is always taken by the independent
risk_gateway service, which applies its own limits regardless of this advice.
"""

from __future__ import annotations

import numpy as np

from .. import indicators as ind
from ..data import periods_per_year
from ..runtime import AgentContext, HandlerResult


def volatility_analyst(ctx: AgentContext) -> HandlerResult:
    df = ctx.ohlcv()
    c = df["close"]
    vol = ind.realised_vol(c.tail(168), periods_per_year(ctx.task.timeframe))
    atr_pct = float(ind.atr(df).iloc[-1] / c.iloc[-1])
    rolling = ind.log_returns(c).rolling(24).std().dropna()
    pct = float((rolling < rolling.iloc[-1]).mean())
    return HandlerResult(
        f"Annualised vol {vol:.0%}, ATR {atr_pct:.2%}, vol percentile {pct:.0%}",
        {"annualised_vol": vol, "atr_pct": atr_pct, "vol_percentile": pct},
    )


def position_sizing_specialist(ctx: AgentContext) -> HandlerResult:
    va = ctx.upstream("volatility-analyst") or ctx.inbox.get("_volatility")
    atr_pct = (
        va.metrics["atr_pct"] if va and va.status == "ok" else volatility_analyst(ctx).metrics["atr_pct"]
    )
    equity = float(ctx.portfolio.get("equity", 1000.0))
    risk_per_trade = float(ctx.task.params.get("risk_per_trade", 0.01))
    stop_distance = max(2 * atr_pct, 0.005)
    stake = equity * risk_per_trade / stop_distance
    stake = min(stake, equity * 0.10)  # advisory cap; the gateway enforces its own
    return HandlerResult(
        f"Stake {stake:.2f} (risk {risk_per_trade:.1%} of {equity:.0f}, stop {stop_distance:.2%})",
        {
            "stake": round(stake, 2),
            "stop_distance_pct": stop_distance,
            "risk_per_trade": risk_per_trade,
            "equity": equity,
        },
    )


def portfolio_risk_analyst(ctx: AgentContext) -> HandlerResult:
    equity = float(ctx.portfolio.get("equity", 1000.0))
    positions = ctx.portfolio.get("positions", {})
    ps = ctx.upstream("position-sizing-specialist")
    new_stake = ps.metrics["stake"] if ps and ps.status == "ok" else 0.0
    exposure = {p: v["amount"] * v["entry_price"] for p, v in positions.items()}
    exposure[ctx.task.pair] = exposure.get(ctx.task.pair, 0.0) + new_stake
    gross = sum(exposure.values())
    rets = ctx.ohlcv()["close"].pct_change().dropna().tail(500)
    var95 = float(-np.percentile(rets, 5) * gross)
    cvar95 = float(-rets[rets <= np.percentile(rets, 5)].mean() * gross)
    ca = ctx.upstream("correlation-analyst")
    avg_corr = ca.metrics["average"] if ca and ca.status == "ok" else None
    concentration = max(exposure.values()) / gross if gross else 0.0
    ok = gross / equity <= 0.5 and (concentration <= 0.8 or len(exposure) == 1)
    return HandlerResult(
        f"Gross exposure {gross / equity:.0%} of equity, 1-bar VaR95 {var95:.2f}, "
        f"{'within' if ok else 'ABOVE'} advisory limits",
        {
            "gross_exposure": gross,
            "exposure_pct": gross / equity,
            "var95": var95,
            "cvar95": cvar95,
            "concentration": concentration,
            "average_correlation": avg_corr,
            "advisory_ok": ok,
        },
    )


def drawdown_monitor(ctx: AgentContext) -> HandlerResult:
    hist = [h["equity"] for h in ctx.portfolio.get("history", [])] or [ctx.portfolio.get("equity", 1000.0)]
    eq = np.array(hist, dtype=float)
    peak = np.maximum.accumulate(eq)
    dd = (eq - peak) / peak
    current, worst = float(dd[-1]), float(dd.min())
    pause = current < -0.05
    return HandlerResult(
        f"Drawdown now {current:.2%}, worst {worst:.2%}; pause={pause}",
        {"current_drawdown": current, "max_drawdown": worst, "pause_recommended": pause, "points": len(eq)},
    )


def stress_testing_specialist(ctx: AgentContext) -> HandlerResult:
    equity = float(ctx.portfolio.get("equity", 1000.0))
    positions = ctx.portfolio.get("positions", {})
    gross = sum(v["amount"] * v["entry_price"] for v in positions.values())
    c = ctx.ohlcv(limit=720)["close"]
    worst_24 = float(c.pct_change(24).min())
    scenarios = {f"shock_{int(s * 100)}pct": gross * s for s in (-0.10, -0.20, -0.35)}
    scenarios["worst_24_bars_hist"] = gross * worst_24
    return HandlerResult(
        f"Worst scenario loss {min(scenarios.values()):.2f} on gross {gross:.2f}",
        {
            "gross": gross,
            "equity": equity,
            "scenarios": {k: round(v, 2) for k, v in scenarios.items()},
            "worst_loss_pct_equity": min(scenarios.values()) / equity,
        },
    )


MIN_TRADES, MIN_SHARPE, MAX_DD = 3, 0.0, -0.25


def independent_risk_reviewer(ctx: AgentContext) -> HandlerResult:
    bt = ctx.upstream("backtesting-specialist")
    if bt is None or bt.status != "ok":
        return HandlerResult(
            "No backtest received: every strategy REJECTED by default", {"verdicts": {}, "approved": []}
        )
    verdicts = {
        name: {"verdict": "REJECT", "reasons": ["no reproducible backtest"]}
        for name in bt.metrics.get("untestable", [])
    }
    for name, m in bt.metrics["results"].items():
        reasons = []
        if m["trades"] < MIN_TRADES:
            reasons.append(f"only {m['trades']} trades")
        if m["sharpe"] <= MIN_SHARPE:
            reasons.append(f"Sharpe {m['sharpe']:.2f} <= {MIN_SHARPE}")
        if m["max_drawdown"] < MAX_DD:
            reasons.append(f"drawdown {m['max_drawdown']:.0%}")
        verdicts[name] = {"verdict": "REJECT" if reasons else "APPROVE", "reasons": reasons}
    approved = [k for k, v in verdicts.items() if v["verdict"] == "APPROVE"]
    return HandlerResult(
        f"Approved {len(approved)}/{len(verdicts)} strategies: {approved}",
        {
            "verdicts": verdicts,
            "approved": approved,
            "standards": {"min_trades": MIN_TRADES, "min_sharpe": MIN_SHARPE, "max_drawdown": MAX_DD},
        },
    )


def compliance_officer(ctx: AgentContext) -> HandlerResult:
    checks = {
        "gateway_reachable": bool(ctx.gateway_health.get("ok")),
        "gateway_paper_only": ctx.gateway_health.get("mode") == "paper",
        "live_trading_disabled": ctx.gateway_health.get("live_trading") is False,
        "no_credentials_in_task": not any(
            k in str(ctx.task.params).lower() for k in ("api_key", "secret", "password", "token")
        ),
    }
    for p in ctx.task.params.get("proposals", []):
        checks[f"reviews:{p.get('proposal_id', '?')}"] = len(p.get("reviews", [])) >= 2
    passed = all(checks.values())
    return HandlerResult(
        f"Compliance {'PASS' if passed else 'FAIL'} ({sum(checks.values())}/{len(checks)})",
        {"checks": checks, "passed": passed},
    )
