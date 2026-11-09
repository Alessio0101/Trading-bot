"""
Trading Strategy specialists (19-28).

Each rule maps OHLCV to a 0/1 long-position series so that the Backtesting
Specialist can test exactly what the strategist proposes.
"""

from __future__ import annotations

import pandas as pd

from .. import indicators as ind
from ..models import Signal
from ..runtime import AgentContext, HandlerResult, NeedsIntegration


def _hold(entry: pd.Series, exit_: pd.Series) -> pd.Series:
    """Turn entry/exit events into a 0/1 position series."""
    state = pd.Series(float("nan"), index=entry.index)
    state[entry] = 1.0
    state[exit_ & ~entry] = 0.0
    return state.ffill().fillna(0.0)


def rule_trend(df: pd.DataFrame) -> pd.Series:
    c = df["close"]
    e20, e50, e200 = ind.ema(c, 20), ind.ema(c, 50), ind.ema(c, 200)
    return ((e20 > e50) & (c > e200)).astype(float)


def rule_mean_reversion(df: pd.DataFrame) -> pd.Series:
    c = df["close"]
    bb = ind.bollinger(c)
    r = ind.rsi(c)
    return _hold((c < bb["lower"]) & (r < 35), (c > bb["mid"]) | (r > 60))


def rule_momentum(df: pd.DataFrame) -> pd.Series:
    roc = df["close"].pct_change(24)
    vol_ok = df["volume"] > df["volume"].rolling(50).mean()
    return _hold((roc > 0.03) & vol_ok, roc < 0)


def rule_breakout(df: pd.DataFrame) -> pd.Series:
    dc = ind.donchian(df).shift()
    return _hold(df["close"] > dc["upper"], df["close"] < ind.donchian(df, 10)["lower"].shift())


def rule_swing(df: pd.DataFrame) -> pd.Series:
    c = df["close"]
    r = ind.rsi(c)
    up = ind.ema(c, 50) > ind.ema(c, 200)
    return _hold(up & (r < 40) & (r > r.shift()), (r > 65) | ~up)


def rule_scalp(df: pd.DataFrame) -> pd.Series:
    tp = (df["high"] + df["low"] + df["close"]) / 3
    vwap = (tp * df["volume"]).rolling(30).sum() / df["volume"].rolling(30).sum()
    dev = (df["close"] - vwap) / vwap
    return _hold(dev < -0.002, dev > 0)


STRATEGY_RULES = {
    "trend_following": rule_trend,
    "mean_reversion": rule_mean_reversion,
    "momentum": rule_momentum,
    "breakout": rule_breakout,
    "swing": rule_swing,
    "scalping": rule_scalp,
}


def _signal_from_rule(ctx: AgentContext, name: str, why: str, confidence: float) -> HandlerResult:
    df = ctx.ohlcv()
    pos = STRATEGY_RULES[name](df)
    price = float(df["close"].iloc[-1])
    a = float(ind.atr(df).iloc[-1])
    now, before = int(pos.iloc[-1]), int(pos.iloc[-2])
    signals: list[Signal] = []
    if now == 1:
        signals.append(
            Signal(
                pair=ctx.task.pair,
                side="long",
                confidence=round(confidence, 2),
                source="",
                rationale=why + (" (fresh entry)" if before == 0 else " (position held)"),
                entry_price=price,
                stop_loss=price - 2 * a,
                take_profit=price + 3 * a,
                strategy=name,
            )
        )
    elif before == 1:
        signals.append(
            Signal(
                pair=ctx.task.pair,
                side="exit",
                confidence=0.6,
                source="",
                rationale=f"{name} rule turned flat",
                entry_price=price,
                strategy=name,
            )
        )
    state = "long" if now else "flat"
    return HandlerResult(
        f"{name}: {state}; {len(signals)} proposal(s)",
        {
            "strategy": name,
            "position": state,
            "fresh_entry": now == 1 and before == 0,
            "atr": a,
            "price": price,
            "exposure_ratio": float(pos.mean()),
        },
        signals,
    )


def trend_following_strategist(ctx: AgentContext) -> HandlerResult:
    ta = ctx.upstream("technical-analyst")
    conf = 0.55 + (0.15 * max(ta.metrics["bias"], 0) if ta and ta.status == "ok" else 0)
    return _signal_from_rule(ctx, "trend_following", "EMA20>EMA50 above EMA200", conf)


def mean_reversion_strategist(ctx: AgentContext) -> HandlerResult:
    return _signal_from_rule(ctx, "mean_reversion", "Close below lower Bollinger with RSI<35", 0.55)


def momentum_strategist(ctx: AgentContext) -> HandlerResult:
    return _signal_from_rule(ctx, "momentum", "24-bar ROC > 3% with volume", 0.6)


def breakout_strategist(ctx: AgentContext) -> HandlerResult:
    res = _signal_from_rule(ctx, "breakout", "Close above 20-bar Donchian high", 0.6)
    cp = ctx.upstream("chart-pattern-analyst")
    if cp and cp.status == "ok":
        for s in res.signals:
            if s.side == "long" and s.entry_price and cp.metrics["support"] < s.entry_price:
                s.stop_loss = max(s.stop_loss or 0, cp.metrics["support"])
    return res


def swing_trading_strategist(ctx: AgentContext) -> HandlerResult:
    return _signal_from_rule(ctx, "swing", "RSI pullback turning up inside uptrend", 0.58)


def scalping_strategist(ctx: AgentContext) -> HandlerResult:
    df = ctx.ohlcv(timeframe="1m", limit=300)
    pos = rule_scalp(df)
    return HandlerResult(
        "scalping (1m, polling only): " + ("long bias" if pos.iloc[-1] else "flat"),
        {
            "strategy": "scalping",
            "position": "long" if pos.iloc[-1] else "flat",
            "note": "Proposals suppressed: no streaming execution (NautilusTrader) installed.",
        },
    )


def event_driven_specialist(ctx: AgentContext) -> HandlerResult:
    sent = ctx.upstream("sentiment-analyst")
    if sent is None or sent.status != "ok":
        raise NeedsIntegration("Needs sentiment-analyst output (which needs headlines).")
    score = sent.metrics["sentiment"]
    tags = {t for h in sent.metrics["scored"] for t in h["tags"]}
    veto = "security" in tags and score < 0
    signals = []
    if not veto and score > 0.4:
        price = float(ctx.ohlcv()["close"].iloc[-1])
        signals.append(
            Signal(
                ctx.task.pair,
                "long",
                round(min(0.5 + score / 3, 0.8), 2),
                "",
                f"Positive event sentiment {score:+.2f} ({', '.join(sorted(tags))})",
                entry_price=price,
                stop_loss=price * 0.96,
                take_profit=price * 1.06,
                strategy="event_driven",
            )
        )
    return HandlerResult(
        f"Event view: sentiment {score:+.2f}, tags {sorted(tags)}, veto={veto}",
        {"sentiment": score, "tags": sorted(tags), "veto": veto},
        signals,
    )


def arbitrage_strategist(ctx: AgentContext) -> HandlerResult:
    raise NeedsIntegration("Needs a second venue price feed; only Kraken public data is connected.")


def options_strategist(ctx: AgentContext) -> HandlerResult:
    raise NeedsIntegration("Needs an options chain source and an options-capable paper broker.")


def pairs_trading_strategist(ctx: AgentContext) -> HandlerResult:
    sa = ctx.upstream("statistical-arbitrage-researcher")
    if sa is None or sa.status != "ok":
        raise NeedsIntegration("Needs statistical-arbitrage-researcher output.")
    z, (a, b) = sa.metrics["zscore"], sa.metrics["pair"]
    signals = []
    if abs(z) > 2:
        cheap = b if z > 0 else a
        price = float(ctx.data.ohlcv(cheap, ctx.task.timeframe, 5)["close"].iloc[-1])
        signals.append(
            Signal(
                cheap,
                "long",
                0.55,
                "",
                f"Spread z={z:+.2f}: buy the cheap leg {cheap} (short leg not possible on spot)",
                entry_price=price,
                stop_loss=price * 0.95,
                take_profit=price * 1.04,
                strategy="pairs",
            )
        )
    return HandlerResult(
        f"Pair {a}/{b} z={z:+.2f}; {len(signals)} proposal(s)", {"zscore": z, "pair": [a, b]}, signals
    )
