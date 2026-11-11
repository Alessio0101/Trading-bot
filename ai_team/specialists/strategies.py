"""
Trading Strategy specialists (19-28).

Each rule maps OHLCV to a 0/1 long-position series so that the Backtesting
Specialist can test exactly what the strategist proposes.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

import numpy as np
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


# Protective levels every rule-based proposal carries to the gateway; the backtest
# (quant.backtest_with_stops) simulates exactly these.
STOP_ATR, TAKE_PROFIT_ATR = 2.0, 3.0

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
                stop_loss=price - STOP_ATR * a,
                take_profit=price + TAKE_PROFIT_ATR * a,
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


KRAKEN_TAKER_BPS = 26.0
COINBASE_TAKER_BPS = 60.0


def arbitrage_strategist(ctx: AgentContext) -> HandlerResult:
    """Compare Kraken with Coinbase net of both venues' taker fees (analysis only)."""
    pair = ctx.task.pair
    try:
        cb = ctx.data.feeds.second_venue([pair]).get(pair)
    except Exception as e:
        raise NeedsIntegration(f"Second venue (Coinbase) unreachable right now: {e}") from e
    if not cb:
        raise NeedsIntegration(f"Coinbase lists no {pair} market")
    book = ctx.data.order_book(pair)
    kr_bid, kr_ask = book["bids"][0][0], book["asks"][0][0]
    fees = KRAKEN_TAKER_BPS + COINBASE_TAKER_BPS
    # Buy where it is cheaper (at the ask), sell where it is dearer (at the bid).
    routes = {
        "buy Kraken / sell Coinbase": (cb["bid"] - kr_ask) / kr_ask * 1e4,
        "buy Coinbase / sell Kraken": (kr_bid - cb["ask"]) / cb["ask"] * 1e4,
    }
    best = max(routes, key=lambda k: routes[k])
    gross = routes[best]
    net = gross - fees
    mid_gap = ((kr_bid + kr_ask) / 2 / ((cb["bid"] + cb["ask"]) / 2) - 1) * 1e4
    return HandlerResult(
        f"{pair}: Kraken vs Coinbase gap {mid_gap:+.1f} bps; best route {best} "
        f"{gross:+.1f} bps gross, {net:+.1f} bps after {fees:.0f} bps fees"
        + (" (opportunity)" if net > 0 else " (no opportunity)"),
        {
            "route": best,
            "gross_bps": gross,
            "net_bps": net,
            "fees_bps": fees,
            "mid_gap_bps": mid_gap,
            "opportunity": net > 0,
            "kraken_bid": kr_bid,
            "kraken_ask": kr_ask,
            "coinbase_bid": cb["bid"],
            "coinbase_ask": cb["ask"],
            "note": "Analysis only: the paper broker trades one venue, so no cross-venue order is proposed.",
        },
    )


_EXPIRY = re.compile(r"^[A-Z]+-(\d{1,2}[A-Z]{3}\d{2})-(\d+(?:d\d+)?)-([CP])$")


def _parse_option(name: str):
    m = _EXPIRY.match(name)
    if not m:
        return None
    expiry = datetime.strptime(m.group(1), "%d%b%y").replace(tzinfo=UTC, hour=8)
    return expiry, float(m.group(2).replace("d", ".")), m.group(3)


def options_strategist(ctx: AgentContext) -> HandlerResult:
    """Implied vs realised volatility and put/call positioning from Deribit (analysis only)."""
    asset = ctx.task.pair.split("/")[0]
    try:
        rows = ctx.data.feeds.options(asset)
    except Exception as e:
        raise NeedsIntegration(f"No option data for {asset}: {e}") from e
    now = datetime.now(UTC)
    parsed = []
    for r in rows:
        p = _parse_option(r.get("instrument_name", ""))
        if p and r.get("mark_iv") and r.get("underlying_price"):
            parsed.append((p[0], p[1], p[2], r))
    expiries = sorted({e for e, *_ in parsed if (e - now).days >= 3})
    if not expiries:
        raise NeedsIntegration(f"No {asset} option expiry at least 3 days out")
    expiry = expiries[0]
    near = [(k, cp, r) for e, k, cp, r in parsed if e == expiry]
    under = float(near[0][2]["underlying_price"])
    atm_strike = min({k for k, _, _ in near}, key=lambda k: abs(k - under))
    atm = [r for k, _, r in near if k == atm_strike]
    atm_iv = float(np.mean([r["mark_iv"] for r in atm]))
    put_oi = sum(r.get("open_interest") or 0 for _, _, cp, r in parsed if cp == "P")
    call_oi = sum(r.get("open_interest") or 0 for _, _, cp, r in parsed if cp == "C")
    pc_ratio = put_oi / call_oi if call_oi else None
    closes = ctx.data.ohlcv(ctx.task.pair, "1h", 24 * 14)["close"]
    rv = ind.realised_vol(closes, 365 * 24) * 100
    premium = atm_iv - rv
    if premium > 10:
        view = "options rich vs realised: a covered call (selling premium) would be favoured"
    elif premium < -5:
        view = "options cheap vs realised: protective puts are inexpensive"
    else:
        view = "options fairly priced vs realised volatility"
    return HandlerResult(
        f"{asset} ATM IV {atm_iv:.1f}% ({(expiry - now).days}d) vs realised {rv:.1f}%; "
        f"put/call OI {pc_ratio:.2f}; {view}"
        if pc_ratio is not None
        else f"{asset} ATM IV {atm_iv:.1f}%",
        {
            "asset": asset,
            "expiry": expiry.date().isoformat(),
            "days_to_expiry": (expiry - now).days,
            "atm_strike": atm_strike,
            "atm_iv_pct": atm_iv,
            "realised_vol_pct": rv,
            "iv_premium_pct": premium,
            "put_call_oi_ratio": pc_ratio,
            "view": view,
            "source": "Deribit",
            "note": "Analysis only: the paper broker is spot-only, so no option trade is proposed.",
        },
    )


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
