"""Go/no-go criteria for considering real money (ai_team.readiness)."""

from datetime import UTC, datetime, timedelta

import pandas as pd

from ai_team import readiness


NOW = datetime(2026, 3, 1, tzinfo=UTC)


def btc(start_price, end_price):
    idx = pd.date_range(NOW - timedelta(days=120), NOW, freq="4h", tz="UTC")
    return pd.DataFrame({"close": [start_price] * (len(idx) - 1) + [end_price]}, index=idx)


def trades(n, wins):
    return [{"pnl": 10.0 if i < wins else -5.0, "strategy": "mean_reversion"} for i in range(n)]


def review(win_rate):
    return {"ETH/USDT": {"backtest": {"mean_reversion": {"win_rate": win_rate}, "swing": {"win_rate": 0.1}}}}


def by_key(result):
    return {c["key"]: c["ok"] for c in result["criteria"]}


def test_new_account_waits_for_data():
    pf = {"created_at": (NOW - timedelta(days=2)).isoformat(), "equity": 10_010.0, "realised": []}
    r = readiness.evaluate(pf, 10_000.0, None, {}, None, NOW)
    k = by_key(r)
    assert k["duration"] is False and k["trades"] is False
    assert k["profit"] is None and k["beats_btc"] is None and k["matches_backtest"] is None
    assert k["reliability"] is None and k["drawdown"] is True
    assert r["ready"] is False and r["met"] == 1 and r["total"] == 7


def test_mature_account_meets_everything():
    pf = {
        "created_at": (NOW - timedelta(days=70)).isoformat(),
        "equity": 10_800.0,
        "max_drawdown_pct": -0.06,
        "realised": trades(35, 21),  # 60 % wins
    }
    runner = {"cycles_completed": 1000, "cycles_failed": 5, "alive": True}
    r = readiness.evaluate(pf, 10_000.0, runner, review(0.65), btc(100.0, 105.0), NOW)
    assert all(by_key(r).values()), r["criteria"]
    assert r["ready"] is True


def test_each_failure_is_reported():
    pf = {
        "created_at": (NOW - timedelta(days=70)).isoformat(),
        "equity": 10_300.0,
        "max_drawdown_pct": -0.12,  # too deep
        "realised": trades(35, 10),  # 29 % wins vs 65 % tested
    }
    runner = {"cycles_completed": 90, "cycles_failed": 10, "alive": True}
    k = by_key(readiness.evaluate(pf, 10_000.0, runner, review(0.65), btc(100.0, 110.0), NOW))
    assert k["profit"] is True
    assert k["beats_btc"] is False  # +3 % vs BTC +10 %
    assert k["drawdown"] is False and k["matches_backtest"] is False and k["reliability"] is False


def test_btc_comparison_uses_price_when_account_started():
    pf = {
        "created_at": (NOW - timedelta(days=200)).isoformat(),
        "equity": 10_500.0,
        "realised": trades(12, 8),
    }
    r = readiness.evaluate(pf, 10_000.0, None, {}, btc(100.0, 50.0), NOW)
    assert by_key(r)["beats_btc"] is None  # no BTC candle from that far back
