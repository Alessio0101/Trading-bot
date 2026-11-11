"""Market Analysis specialists (1-10)."""

from __future__ import annotations

import re
from typing import Any

import numpy as np

from .. import indicators as ind
from ..feeds import FeedUnavailable
from ..runtime import AgentContext, HandlerResult, NeedsIntegration


def technical_analyst(ctx: AgentContext) -> HandlerResult:
    df = ctx.ohlcv()
    c = df["close"]
    e20, e50, e200 = (float(ind.ema(c, p).iloc[-1]) for p in (20, 50, 200))
    r = float(ind.rsi(c).iloc[-1])
    m = ind.macd(c).iloc[-1]
    price = float(c.iloc[-1])
    votes = [price > e20, e20 > e50, e50 > e200, m["hist"] > 0, r > 50]
    bias = round((sum(votes) - (len(votes) - sum(votes))) / len(votes), 2)
    trend = "up" if bias >= 0.6 else "down" if bias <= -0.6 else "sideways"
    flag = "overbought" if r > 70 else "oversold" if r < 30 else "neutral"
    return HandlerResult(
        f"{ctx.task.pair} trend {trend} (bias {bias:+.2f}), RSI {r:.1f} {flag}",
        {
            "price": price,
            "ema20": e20,
            "ema50": e50,
            "ema200": e200,
            "rsi": r,
            "macd_hist": float(m["hist"]),
            "bias": bias,
            "trend": trend,
            "rsi_state": flag,
        },
    )


def _swings(series, window: int, mode: str) -> list[int]:
    roll = series.rolling(2 * window + 1, center=True)
    ext = roll.max() if mode == "high" else roll.min()
    return [i for i, (v, e) in enumerate(zip(series, ext, strict=True)) if v == e]


def chart_pattern_analyst(ctx: AgentContext) -> HandlerResult:
    df = ctx.ohlcv(limit=300)
    price = float(df["close"].iloc[-1])
    highs = [float(df["high"].iloc[i]) for i in _swings(df["high"], 5, "high")]
    lows = [float(df["low"].iloc[i]) for i in _swings(df["low"], 5, "low")]
    resistance = min((h for h in highs if h > price), default=float(df["high"].max()))
    support = max((low for low in lows if low < price), default=float(df["low"].min()))
    structure = "range"
    if len(highs) >= 2 and len(lows) >= 2:
        if highs[-1] > highs[-2] and lows[-1] > lows[-2]:
            structure = "higher-highs/higher-lows"
        elif highs[-1] < highs[-2] and lows[-1] < lows[-2]:
            structure = "lower-highs/lower-lows"
    return HandlerResult(
        f"Support {support:.6g}, resistance {resistance:.6g}, structure {structure}",
        {
            "price": price,
            "support": support,
            "resistance": resistance,
            "structure": structure,
            "to_support_pct": (price - support) / price * 100,
            "to_resistance_pct": (resistance - price) / price * 100,
            "swing_highs": len(highs),
            "swing_lows": len(lows),
        },
    )


EVENT_TAGS = {
    "regulation": ["sec", "regulat", "ban", "lawsuit", "etf", "approval", "law"],
    "security": ["hack", "exploit", "breach", "stolen"],
    "listing": ["listing", "lists", "delist"],
    "macro": ["fed", "rate", "inflation", "cpi", "recession"],
    "adoption": ["partnership", "adopt", "integration", "integrated", "launch"],
}


def _base(pair: str) -> str:
    return pair.split("/")[0]


def news_analyst(ctx: AgentContext) -> HandlerResult:
    """Classify headlines from the public RSS feeds (or headlines given in the task)."""
    given = ctx.task.params.get("headlines")
    if given:
        items = [{"headline": h, "source": "task", "published": None} for h in given]
        meta: dict[str, Any] = {"asset_specific": True, "sources": ["task"], "feed_errors": {}}
    else:
        try:
            news = ctx.data.feeds.news(_base(ctx.task.pair))
        except Exception as e:
            raise NeedsIntegration(f"News feeds unreachable right now: {e}") from e
        items = news["items"]
        meta = {
            "asset_specific": news["asset_specific"],
            "sources": news["sources"],
            "feed_errors": news["errors"],
        }
    tagged = []
    for item in items:
        low = item["headline"].lower()
        tags = [t for t, kws in EVENT_TAGS.items() if any(k in low for k in kws)] or ["general"]
        tagged.append({**item, "tags": tags})
    scope = _base(ctx.task.pair) if meta["asset_specific"] else "crypto market (no asset-specific headlines)"
    return HandlerResult(
        f"Classified {len(tagged)} headlines about {scope} from {', '.join(meta['sources'])}",
        {"headlines": tagged, **meta},
    )


POSITIVE = {
    "approval",
    "approved",
    "surge",
    "rally",
    "gain",
    "record",
    "adopt",
    "partnership",
    "bullish",
    "inflow",
    "upgrade",
    "launch",
    "growth",
    "beat",
    "rise",
    "rises",
    "jumps",
    "soars",
    "rebound",
    "recovers",
    "record-high",
    "breakout",
    "accumulate",
    "buying",
    "inflows",
    "approves",
    "wins",
}
NEGATIVE = {
    "hack",
    "exploit",
    "ban",
    "lawsuit",
    "crash",
    "plunge",
    "bearish",
    "outflow",
    "delist",
    "fraud",
    "stolen",
    "fall",
    "drop",
    "downgrade",
    "recession",
    "fear",
    "falls",
    "drops",
    "slides",
    "slump",
    "sell-off",
    "selloff",
    "liquidations",
    "outflows",
    "sues",
    "charges",
    "warning",
    "decline",
    "dump",
    "losses",
}


def _lexicon_score(headline: str) -> float:
    words = re.findall(r"[a-z]+", headline.lower())
    pos = sum(w in POSITIVE for w in words)
    neg = sum(w in NEGATIVE for w in words)
    return 0.0 if pos == neg else (pos - neg) / (pos + neg)


def _text_sentiment(ctx: AgentContext, texts: list[str], kind: str) -> tuple[float, int]:
    """Mean sentiment of texts: Claude's scores where available, the lexicon otherwise."""
    if not texts:
        return 0.0, 0
    scorer = getattr(ctx.data.feeds, "headline_scores", None)
    try:
        claude = scorer(texts, kind) if scorer else {}
    except Exception:  # scoring is optional
        claude = {}
    scores = [claude[t]["sentiment"] if t in claude else _lexicon_score(t) for t in texts]
    return float(np.mean(scores)), sum(1 for t in texts if t in claude)


VIEW_THRESHOLD = 0.2  # a source "has a view" when its score is beyond +/- this


def sentiment_analyst(ctx: AgentContext) -> HandlerResult:
    """Crowd sentiment from news, Reddit, podcasts, the Fear & Greed index and futures funding.

    Each source gives a score in [-1, 1]. Fear & Greed and funding are read contrarian:
    extreme fear or shorts paying longs is bullish, extreme greed or crowded longs is
    bearish. The sources are combined with weights learned from their own track record
    (ai_team.track_record, "source:<name>"), passed in by the coordinator.
    """
    news = ctx.upstream("news-analyst")
    if news is None or news.status != "ok":
        raise NeedsIntegration("Needs classified headlines from news-analyst.")
    items = news.metrics["headlines"]
    scorer = getattr(ctx.data.feeds, "headline_scores", None)
    try:
        claude = scorer([i["headline"] for i in items]) if scorer else {}
    except Exception:  # scoring is optional: fall back to the lexicon
        claude = {}
    scored = []
    for item in items:
        c = claude.get(item["headline"])
        if c is not None:
            scored.append({**item, "score": c["sentiment"], "event": c["event"], "method": "claude"})
        else:
            scored.append({**item, "score": _lexicon_score(item["headline"]), "method": "lexicon"})
    news_score = float(np.mean([s["score"] for s in scored])) if scored else 0.0
    by_claude = sum(1 for s in scored if s["method"] == "claude")
    method = "claude" if scored and by_claude == len(scored) else "mixed" if by_claude else "lexicon"

    components: dict[str, float] = {"news": news_score}
    details: dict[str, Any] = {}
    errors: dict[str, str] = {}
    feeds = ctx.data.feeds
    asset = _base(ctx.task.pair)
    try:
        posts = feeds.reddit()["posts"]
        components["reddit"], n = _text_sentiment(ctx, [p["title"] for p in posts], "reddit")
        details["reddit"] = {"posts": len(posts), "scored_by_claude": n}
    except Exception as e:
        errors["reddit"] = str(e)[:200]
    try:
        eps = feeds.podcasts()["episodes"]
        texts = [f"{e['show']}: {e['title']}. {e['notes']}"[:500] for e in eps]
        components["podcasts"], n = _text_sentiment(ctx, texts, "podcasts")
        details["podcasts"] = {
            "episodes": len(eps),
            "scored_by_claude": n,
            "titles": [e["title"] for e in eps][:10],
        }
    except Exception as e:
        errors["podcasts"] = str(e)[:200]
    try:
        fg = feeds.fear_greed()
        components["fear_greed"] = max(-1.0, min(1.0, (50 - fg["value"]) / 40))  # contrarian
        details["fear_greed"] = fg
    except Exception as e:
        errors["fear_greed"] = str(e)[:200]
    try:
        d = feeds.derivatives().get(asset)
        if d is None:
            raise FeedUnavailable(f"no perpetual future for {asset}")
        # Crowded longs pay high funding: contrarian bearish. +/-30 % a year saturates.
        components["funding"] = max(-1.0, min(1.0, -d["funding_annualised"] / 0.3))
        details["funding"] = d
    except Exception as e:
        errors["funding"] = str(e)[:200]

    weights = {
        k: float((ctx.task.params.get("source_weights") or {}).get(f"source:{k}", 1.0)) for k in components
    }
    composite = sum(components[k] * weights[k] for k in components) / sum(weights.values())
    views = {
        k: (1 if v > VIEW_THRESHOLD else -1 if v < -VIEW_THRESHOLD else 0) for k, v in components.items()
    }
    parts = ", ".join(f"{k} {v:+.2f}" for k, v in components.items())
    return HandlerResult(
        f"Sentiment {composite:+.2f} ({parts}); {by_claude}/{len(scored)} headlines scored by Claude",
        {
            "sentiment": composite,
            "news_sentiment": news_score,
            "components": components,
            "source_weights": weights,
            "source_views": views,
            "sources": details,
            "source_errors": errors,
            "scored": scored,
            "method": method,
            "claude_scored": by_claude,
        },
    )


def _economic_bias(e: dict[str, Any]) -> tuple[float, list[str]]:
    """-1 (headwind for risk assets) .. +1 (tailwind) from rates, inflation, yields and the dollar."""
    bias, notes = 0.0, []
    for value, threshold, weight, up, down in (
        (e["fed_funds_3m_change"], 0.1, 0.3, "Fed tightening", "Fed easing"),
        (e["cpi_yoy_pct"] - e["cpi_yoy_3m_ago_pct"], 0.2, 0.2, "inflation rising", "inflation falling"),
        (e["us10y_1m_change"], 0.25, 0.2, "10y yield rising", "10y yield falling"),
        (e["dollar_1m_change_pct"], 2.0, 0.2, "dollar strengthening", "dollar weakening"),
    ):
        if value > threshold:
            bias -= weight
            notes.append(up)
        elif value < -threshold:
            bias += weight
            notes.append(down)
    return bias, notes


def macro_economic_analyst(ctx: AgentContext) -> HandlerResult:
    """Crypto-wide risk appetite (CoinGecko) plus US rates, inflation, yields and dollar (FRED)."""
    try:
        g = ctx.data.feeds.global_market()
    except Exception as e:
        raise NeedsIntegration(f"Global market data unreachable right now: {e}") from e
    change = float(g.get("market_cap_change_percentage_24h_usd") or 0.0)
    btc_dom = float((g.get("market_cap_percentage") or {}).get("btc") or 0.0)
    regime = "risk-on" if change > 1.5 else "risk-off" if change < -1.5 else "neutral"
    total = float((g.get("total_market_cap") or {}).get("usd") or 0.0)
    crypto_appetite = max(-1.0, min(1.0, change / 5))
    try:
        econ: dict[str, Any] | None = ctx.data.feeds.economic()
    except Exception as e:
        econ, econ_error = None, str(e)
    else:
        econ_error = None
    metrics: dict[str, Any] = {
        "regime": regime,
        "total_market_cap_usd": total,
        "market_cap_change_24h_pct": change,
        "btc_dominance_pct": btc_dom,
    }
    summary = (
        f"Crypto market {regime}: total cap {total / 1e12:.2f}T USD ({change:+.2f}% 24h), "
        f"BTC dominance {btc_dom:.1f}%"
    )
    if econ:
        bias, notes = _economic_bias(econ)
        metrics |= {
            "economic": econ,
            "economic_bias": bias,
            "economic_notes": notes,
            "risk_appetite": max(-1.0, min(1.0, 0.6 * crypto_appetite + 0.4 * bias)),
            "scope": "crypto market + US economy (FRED)",
        }
        summary += (
            f"; Fed funds {econ['fed_funds_pct']:.2f}%, CPI {econ['cpi_yoy_pct']:.1f}% y/y, "
            f"10y {econ['us10y_pct']:.2f}%: macro {bias:+.1f} ({', '.join(notes) or 'steady'})"
        )
    else:
        metrics |= {
            "risk_appetite": crypto_appetite,
            "scope": "crypto market only; economic data unavailable",
            "economic_error": econ_error,
        }
    return HandlerResult(summary, metrics)


def fundamental_analyst(ctx: AgentContext) -> HandlerResult:
    """Tokenomics and valuation health from CoinGecko market data."""
    asset = _base(ctx.task.pair)
    try:
        f = ctx.data.feeds.fundamentals([asset]).get(asset)
    except Exception as e:
        raise NeedsIntegration(f"Fundamental data unreachable right now: {e}") from e
    if not f:
        raise NeedsIntegration(f"No fundamental data for {asset}")
    mcap = float(f.get("market_cap") or 0)
    fdv = float(f.get("fully_diluted_valuation") or mcap or 0)
    circ = float(f.get("circulating_supply") or 0)
    cap = float(f.get("max_supply") or f.get("total_supply") or circ or 0)
    turnover = float(f.get("total_volume") or 0) / mcap if mcap else 0.0
    circulating_ratio = circ / cap if cap else 1.0
    dilution = fdv / mcap if mcap else 1.0
    ath_gap = float(f.get("ath_change_percentage") or 0)
    ch30 = float(f.get("price_change_percentage_30d_in_currency") or 0)
    # Score 0..1: more circulating supply, less future dilution and healthy turnover score higher.
    score = (
        0.4 * min(circulating_ratio, 1.0)
        + 0.3 * min(1.0, 1 / max(dilution, 1.0))
        + 0.3 * min(turnover / 0.05, 1.0)
    )
    return HandlerResult(
        f"{asset} fundamentals score {score:.2f}: {circulating_ratio:.0%} of supply circulating, "
        f"FDV/market cap {dilution:.2f}, turnover {turnover:.1%}/day, {ath_gap:+.0f}% from ATH",
        {
            "asset": asset,
            "fundamental_score": round(score, 3),
            "market_cap_usd": mcap,
            "fdv_usd": fdv,
            "circulating_ratio": circulating_ratio,
            "fdv_to_market_cap": dilution,
            "daily_turnover": turnover,
            "ath_change_pct": ath_gap,
            "change_30d_pct": ch30,
            "rank": f.get("market_cap_rank"),
            "source": "CoinGecko",
        },
    )


def volume_analyst(ctx: AgentContext) -> HandlerResult:
    df = ctx.ohlcv()
    v = df["volume"]
    rel = float(v.iloc[-1] / v.tail(50).mean())
    z = float((v.iloc[-1] - v.tail(50).mean()) / (v.tail(50).std() or 1))
    o = ind.obv(df)
    slope = float(np.polyfit(np.arange(20), o.tail(20).to_numpy(), 1)[0])
    return HandlerResult(
        f"Relative volume {rel:.2f}x, z {z:+.2f}, OBV {'rising' if slope > 0 else 'falling'}",
        {
            "relative_volume": rel,
            "volume_z": z,
            "obv_slope": slope,
            "anomaly": abs(z) > 2.5,
            "avg_volume_50": float(v.tail(50).mean()),
        },
    )


def _depth_within(levels, mid: float, pct: float, side: str) -> float:
    if side == "bid":
        return sum(p * q for p, q in levels if p >= mid * (1 - pct))
    return sum(p * q for p, q in levels if p <= mid * (1 + pct))


def liquidity_analyst(ctx: AgentContext) -> HandlerResult:
    book = ctx.data.order_book(ctx.task.pair)
    bid, ask = book["bids"][0][0], book["asks"][0][0]
    mid = (bid + ask) / 2
    spread_bps = (ask - bid) / mid * 1e4
    d05 = _depth_within(book["bids"], mid, 0.005, "bid") + _depth_within(book["asks"], mid, 0.005, "ask")
    d1 = _depth_within(book["bids"], mid, 0.01, "bid") + _depth_within(book["asks"], mid, 0.01, "ask")
    vol = ctx.upstream("volume-analyst")
    rel_vol = vol.metrics.get("relative_volume") if vol and vol.status == "ok" else None
    # If the deepest level fetched is still inside the band, depth is a lower bound.
    reach = min(mid - book["bids"][-1][0], book["asks"][-1][0] - mid) / mid
    truncated = {"0.5pct": reach < 0.005, "1pct": reach < 0.01}
    score = max(0.0, min(1.0, (1 - spread_bps / 50) * 0.6 + min(d1 / 1e6, 1) * 0.4))
    return HandlerResult(
        f"Spread {spread_bps:.2f} bps, depth ±1% {d1:,.0f} quote, score {score:.2f}",
        {
            "mid": mid,
            "spread_bps": spread_bps,
            "depth_0_5pct": d05,
            "depth_1pct": d1,
            "liquidity_score": score,
            "relative_volume": rel_vol,
            "book_levels": len(book["bids"]),
            "book_reach_pct": reach * 100,
            "depth_is_lower_bound": truncated,
        },
    )


def order_flow_analyst(ctx: AgentContext) -> HandlerResult:
    trades = ctx.data.trades(ctx.task.pair)
    notional = trades["price"] * trades["amount"]
    buy, sell = notional[trades["side"] == "buy"].sum(), notional[trades["side"] == "sell"].sum()
    trade_imb = float((buy - sell) / (buy + sell)) if buy + sell else 0.0
    book = ctx.data.order_book(ctx.task.pair)
    bq = sum(q for _, q in book["bids"][:10])
    aq = sum(q for _, q in book["asks"][:10])
    book_imb = float((bq - aq) / (bq + aq)) if bq + aq else 0.0
    liq = ctx.upstream("liquidity-analyst")
    weight = liq.metrics["liquidity_score"] if liq and liq.status == "ok" else 0.5
    bias = round((0.6 * trade_imb + 0.4 * book_imb) * (0.5 + weight / 2), 3)
    return HandlerResult(
        f"Trade imbalance {trade_imb:+.2f}, book imbalance {book_imb:+.2f}, flow bias {bias:+.2f}",
        {
            "trade_imbalance": trade_imb,
            "book_imbalance": book_imb,
            "flow_bias": bias,
            "liquidity_weight": weight,
            "trades_used": len(trades),
        },
    )


def market_regime_analyst(ctx: AgentContext) -> HandlerResult:
    c = ctx.ohlcv()["close"]
    window = 48
    change = abs(c.iloc[-1] - c.iloc[-window])
    path = c.diff().abs().tail(window - 1).sum()
    er = float(change / path) if path else 0.0
    vol = ind.log_returns(c).rolling(24).std().dropna()
    pct = float((vol < vol.iloc[-1]).mean()) if len(vol) else 0.5
    regime = ("trending" if er > 0.3 else "ranging") + "/" + ("volatile" if pct > 0.7 else "calm")
    return HandlerResult(
        f"Regime {regime} (efficiency {er:.2f}, vol pct {pct:.0%})",
        {"regime": regime, "efficiency_ratio": er, "vol_percentile": pct, "trending": er > 0.3},
    )
