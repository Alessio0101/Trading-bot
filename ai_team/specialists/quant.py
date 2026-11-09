"""Quantitative Research specialists (11-18)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from .. import indicators as ind
from ..data import periods_per_year
from ..runtime import AgentContext, HandlerResult, NeedsIntegration
from .strategies import STRATEGY_RULES


FEE = 0.0026  # Kraken taker fee per side, conservative
SLIPPAGE = 0.0005  # 5 bps per side, matches the gateway's assumed slippage


def backtest(
    df: pd.DataFrame, position: pd.Series, timeframe: str, fee: float = FEE, slippage: float = SLIPPAGE
) -> dict:
    """Vectorised long-only backtest; positions act on the next bar (no look-ahead)."""
    pos = position.shift().fillna(0)
    rets = df["close"].pct_change().fillna(0)
    costs = pos.diff().abs().fillna(0) * (fee + slippage)
    strat = pos * rets - costs
    equity = (1 + strat).cumprod()
    trades = int((pos.diff() > 0).sum())
    ppy = periods_per_year(timeframe)
    return {
        "total_return": float(equity.iloc[-1] - 1),
        "sharpe": ind.sharpe(strat, ppy),
        "max_drawdown": ind.max_drawdown(equity),
        "trades": trades,
        "exposure": float(pos.mean()),
        "buy_hold_return": float(df["close"].iloc[-1] / df["close"].iloc[0] - 1),
        "bars": len(df),
    }


HYPOTHESES = {
    "momentum_24": lambda c: c.pct_change(24),
    "reversal_6": lambda c: -c.pct_change(6),
    "trend_ema": lambda c: ind.ema(c, 20) / ind.ema(c, 50) - 1,
}


def quantitative_researcher(ctx: AgentContext) -> HandlerResult:
    c = ctx.ohlcv(limit=720)["close"]
    fwd = c.pct_change(6).shift(-6)
    results = {}
    for name, f in HYPOTHESES.items():
        sig = f(c)
        ok = sig.notna() & fwd.notna()
        ic = float(sig[ok].rank().corr(fwd[ok].rank())) if ok.sum() > 30 else 0.0  # Spearman
        hit = float((np.sign(sig[ok]) == np.sign(fwd[ok])).mean()) if ok.any() else 0.0
        results[name] = {"ic": round(ic, 4), "hit_rate": round(hit, 3)}
    best = max(results, key=lambda k: results[k]["ic"])
    # Hypotheses map to strategy rules the backtester can test.
    candidates = ["trend_following", "momentum", "mean_reversion", "breakout", "swing"]
    return HandlerResult(
        f"Best hypothesis {best} (IC {results[best]['ic']:+.3f}); "
        f"sending {len(candidates)} candidate strategies to backtesting",
        {"hypotheses": results, "best": best, "candidate_strategies": candidates},
    )


def statistical_arbitrage_researcher(ctx: AgentContext) -> HandlerResult:
    a, b = ctx.task.params.get("pair_a", "BTC/USDT"), ctx.task.params.get("pair_b", "ETH/USDT")
    la = np.log(ctx.data.ohlcv(a, ctx.task.timeframe, 500)["close"])
    lb = np.log(ctx.data.ohlcv(b, ctx.task.timeframe, 500)["close"])
    n = min(len(la), len(lb))
    la, lb = la.to_numpy()[-n:], lb.to_numpy()[-n:]
    beta, alpha = np.polyfit(lb, la, 1)
    spread = la - (alpha + beta * lb)
    z = float((spread[-1] - spread.mean()) / (spread.std() or 1))
    lag, delta = spread[:-1], np.diff(spread)
    theta = np.polyfit(lag - lag.mean(), delta, 1)[0]
    half_life = float(-np.log(2) / theta) if theta < 0 else float("inf")
    return HandlerResult(
        f"{a}~{b}: beta {beta:.3f}, z {z:+.2f}, half-life {half_life:.1f} bars",
        {
            "pair": [a, b],
            "hedge_ratio": float(beta),
            "zscore": z,
            "half_life_bars": half_life,
            "mean_reverting": theta < 0,
        },
    )


def factor_researcher(ctx: AgentContext) -> HandlerResult:
    rows = {}
    for p in ctx.universe:
        df = ctx.data.ohlcv(p, ctx.task.timeframe, 300)
        c = df["close"]
        rows[p] = {
            "momentum": float(c.iloc[-1] / c.iloc[-72] - 1),
            "low_vol": -float(ind.log_returns(c).tail(72).std()),
            "volume_trend": float(df["volume"].tail(24).mean() / df["volume"].tail(168).mean() - 1),
        }
    table = pd.DataFrame(rows).T
    composite = table.rank(pct=True).mean(axis=1).sort_values(ascending=False)
    return HandlerResult(
        f"Top factor rank: {composite.index[0]}",
        {
            "factors": table.round(5).to_dict("index"),
            "ranking": [[k, round(v, 3)] for k, v in composite.items()],
        },
    )


def machine_learning_researcher(ctx: AgentContext) -> HandlerResult:
    from ..integrations import status as integ

    if not (integ("qlib")["installed"] or integ("rdagent")["installed"]):
        raise NeedsIntegration("Model training needs Qlib or RD-Agent; neither is installed.")
    raise NeedsIntegration("Qlib/RD-Agent detected but the training pipeline is not wired yet.")


def backtesting_specialist(ctx: AgentContext) -> HandlerResult:
    qr = ctx.upstream("quantitative-researcher")
    names = (
        qr.metrics["candidate_strategies"]
        if qr and qr.status == "ok"
        else ctx.task.params.get("strategies", list(STRATEGY_RULES))
    )
    df = ctx.ohlcv(limit=720)
    # Discretionary strategies (event-driven, pairs) have no deterministic rule to
    # replay; they are reported as untestable so the reviewer rejects them.
    untestable = sorted(n for n in names if n not in STRATEGY_RULES)
    results = {
        n: backtest(df, STRATEGY_RULES[n](df), ctx.task.timeframe) for n in names if n in STRATEGY_RULES
    }
    best = max(results, key=lambda k: results[k]["sharpe"]) if results else None
    best_txt = f"best {best} (Sharpe {results[best]['sharpe']:.2f})" if best else "nothing testable"
    return HandlerResult(
        f"Backtested {len(results)} strategies on {len(df)} bars; {best_txt}"
        + (f"; untestable: {untestable}" if untestable else ""),
        {
            "results": results,
            "best": best,
            "untestable": untestable,
            "timeframe": ctx.task.timeframe,
            "fee": FEE,
            "slippage": SLIPPAGE,
        },
    )


def data_engineer(ctx: AgentContext) -> HandlerResult:
    df = ctx.ohlcv()
    gaps = int((df.index.to_series().diff().dropna() != df.index.to_series().diff().mode()[0]).sum())
    issues = {
        "rows": len(df),
        "nan_values": int(df.isna().sum().sum()),
        "non_positive_prices": int((df[["open", "high", "low", "close"]] <= 0).sum().sum()),
        "high_below_low": int((df["high"] < df["low"]).sum()),
        "timestamp_gaps": gaps,
        "last_candle": str(df.index[-1]),
        "source": ctx.data.name,
    }
    ok = issues["nan_values"] == 0 and issues["non_positive_prices"] == 0 and issues["high_below_low"] == 0
    issues["quality_ok"] = ok
    return HandlerResult(
        f"{issues['rows']} rows from {ctx.data.name}, quality {'OK' if ok else 'FAILED'}, gaps {gaps}", issues
    )


def feature_engineer(ctx: AgentContext) -> HandlerResult:
    de = ctx.upstream("data-engineer")
    if de and de.status == "ok" and not de.metrics.get("quality_ok", True):
        raise RuntimeError("Upstream data quality check failed; refusing to build features.")
    df = ctx.ohlcv()
    c = df["close"]
    feats = pd.DataFrame(
        {
            "ret_1": c.pct_change(),
            "ret_24": c.pct_change(24),
            "vol_24": ind.log_returns(c).rolling(24).std(),
            "rsi_14": ind.rsi(c),
            "ema_ratio": ind.ema(c, 20) / ind.ema(c, 50) - 1,
            "vol_ratio": df["volume"] / df["volume"].rolling(50).mean(),
        }
    ).dropna()
    return HandlerResult(
        f"Built {feats.shape[1]} features x {feats.shape[0]} rows",
        {
            "features": list(feats.columns),
            "rows": int(feats.shape[0]),
            "latest": feats.iloc[-1].round(6).to_dict(),
        },
    )


def correlation_analyst(ctx: AgentContext) -> HandlerResult:
    rets = pd.DataFrame(
        {p: ctx.data.ohlcv(p, ctx.task.timeframe, 300)["close"].pct_change() for p in ctx.universe}
    ).dropna()
    corr = rets.corr()
    off = corr.where(~np.eye(len(corr), dtype=bool)).stack()
    high = [[a, b, round(v, 3)] for (a, b), v in off.items() if v > 0.8 and a < b]
    return HandlerResult(
        f"Average correlation {off.mean():.2f}; {len(high)} highly correlated pairs",
        {"matrix": corr.round(3).to_dict(), "average": float(off.mean()), "high_pairs": high},
    )
