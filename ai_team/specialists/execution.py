"""Execution Trading specialists (29-35). They plan paper orders; they never place them."""

from __future__ import annotations

from ..runtime import AgentContext, HandlerResult


FEE_BPS = 26.0  # Kraken taker, conservative


def _proposal(ctx: AgentContext) -> dict:
    if "proposal" in ctx.task.params:
        return ctx.task.params["proposal"]
    agg = ctx.upstream("signal-aggregation-specialist")
    if agg and agg.status == "ok" and agg.metrics.get("proposals"):
        return agg.metrics["proposals"][0]
    price = float(ctx.ohlcv(limit=5)["close"].iloc[-1])
    return {
        "pair": ctx.task.pair,
        "side": "long",
        "entry_price": price,
        "stake": ctx.task.params.get("stake", 100.0),
    }


def execution_trader(ctx: AgentContext) -> HandlerResult:
    p = _proposal(ctx)
    book = ctx.data.order_book(p["pair"])
    bid, ask = book["bids"][0][0], book["asks"][0][0]
    side = "buy" if p["side"] == "long" else "sell"
    limit = ask if side == "buy" else bid  # marketable limit at the touch
    stake = float(p.get("stake") or ctx.task.params.get("stake", 100.0))
    plan = {
        "pair": p["pair"],
        "side": side,
        "type": "limit",
        "time_in_force": "IOC",
        "limit_price": limit,
        "stake": stake,
        "amount": stake / limit,
        "venue": "paper",
    }
    return HandlerResult(
        f"Plan: {side} {plan['amount']:.6g} {p['pair']} @ {limit:.6g} IOC (paper)", {"order_plan": plan}
    )


def order_routing_specialist(ctx: AgentContext) -> HandlerResult:
    return HandlerResult(
        "Routed to 'paper' (only venue available; live venues are disabled)",
        {"venue": "paper", "candidates": ["paper"], "live_venues_enabled": False},
    )


def _walk(levels, quote_amount: float) -> tuple[float, float]:
    filled_q, filled_b = 0.0, 0.0
    for price, qty in levels:
        take = min(qty * price, quote_amount - filled_q)
        filled_q += take
        filled_b += take / price
        if filled_q >= quote_amount - 1e-9:
            break
    return filled_q, (filled_q / filled_b if filled_b else levels[0][0])


def slippage_analyst(ctx: AgentContext) -> HandlerResult:
    et = ctx.upstream("execution-trader")
    plan = (
        et.metrics["order_plan"] if et and et.status == "ok" else execution_trader(ctx).metrics["order_plan"]
    )
    book = ctx.data.order_book(plan["pair"])
    levels = book["asks"] if plan["side"] == "buy" else book["bids"]
    filled, avg = _walk(levels, plan["stake"])
    touch = levels[0][0]
    slip = abs(avg - touch) / touch * 1e4
    return HandlerResult(
        f"Expected slippage {slip:.2f} bps for {plan['stake']:.2f} quote",
        {
            "slippage_bps": slip,
            "avg_fill": avg,
            "touch": touch,
            "fully_fillable": filled >= plan["stake"] - 1e-6,
            "stake": plan["stake"],
        },
    )


def transaction_cost_analyst(ctx: AgentContext) -> HandlerResult:
    sl = ctx.upstream("slippage-analyst")
    slip = sl.metrics["slippage_bps"] if sl and sl.status == "ok" else 0.0
    book = ctx.data.order_book(ctx.task.pair)
    mid = (book["bids"][0][0] + book["asks"][0][0]) / 2
    half_spread = (book["asks"][0][0] - book["bids"][0][0]) / mid * 1e4 / 2
    round_trip = 2 * (FEE_BPS + half_spread + slip)
    edge_bps = ctx.task.params.get("expected_edge_bps", 150.0)
    return HandlerResult(
        f"Round-trip cost {round_trip:.1f} bps vs edge {edge_bps:.0f} bps",
        {
            "fee_bps": FEE_BPS,
            "half_spread_bps": half_spread,
            "slippage_bps": slip,
            "round_trip_cost_bps": round_trip,
            "cost_to_edge": round_trip / edge_bps,
            "acceptable": round_trip < edge_bps * 0.5,
        },
    )


def market_microstructure_analyst(ctx: AgentContext) -> HandlerResult:
    book = ctx.data.order_book(ctx.task.pair)
    (bp, bq), (ap, aq) = book["bids"][0], book["asks"][0]
    mid = (bp + ap) / 2
    micro = (bp * aq + ap * bq) / (aq + bq)
    bid_slope = sum(q for _, q in book["bids"][:10]) / max(abs(book["bids"][9][0] - bp), 1e-12)
    ask_slope = sum(q for _, q in book["asks"][:10]) / max(abs(book["asks"][9][0] - ap), 1e-12)
    trades = ctx.data.trades(ctx.task.pair)
    return HandlerResult(
        f"Microprice {(micro - mid) / mid * 1e4:+.2f} bps from mid",
        {
            "mid": mid,
            "microprice": micro,
            "spread_bps": (ap - bp) / mid * 1e4,
            "bid_slope": bid_slope,
            "ask_slope": ask_slope,
            "avg_trade_size": float(trades["amount"].mean()),
        },
    )


def algorithmic_execution_specialist(ctx: AgentContext) -> HandlerResult:
    stake = float(ctx.task.params.get("stake", 1000.0))
    slices = int(ctx.task.params.get("slices", 6))
    df = ctx.ohlcv(timeframe="1h", limit=24 * 7)
    profile = df["volume"].groupby(df.index.hour).mean()
    hours = [(df.index[-1].hour + i + 1) % 24 for i in range(slices)]
    w = profile.reindex(hours).fillna(profile.mean())
    w = w / w.sum()
    schedule = [
        {"hour_utc": int(h), "stake": round(stake * float(x), 2)} for h, x in zip(hours, w, strict=True)
    ]
    return HandlerResult(
        f"VWAP schedule: {slices} child orders over {slices}h",
        {"algorithm": "VWAP", "schedule": schedule, "total": stake},
    )


def trade_reconciliation_specialist(ctx: AgentContext) -> HandlerResult:
    approved = [d for d in ctx.decisions if d.get("type") == "gateway" and d.get("approved")]
    fills = ctx.portfolio.get("fills", [])
    fill_ids = {f.get("proposal_id") for f in fills}
    missing = [d["proposal_id"] for d in approved if d.get("proposal_id") not in fill_ids]
    unknown = [
        f["proposal_id"]
        for f in fills
        if f.get("proposal_id") not in {d.get("proposal_id") for d in approved}
    ]
    ok = not missing and not unknown
    return HandlerResult(
        f"Reconciled {len(approved)} approvals vs {len(fills)} fills: {'OK' if ok else 'MISMATCH'}",
        {
            "approved": len(approved),
            "fills": len(fills),
            "approved_without_fill": missing,
            "fill_without_approval": unknown,
            "reconciled": ok,
        },
    )
