"""Weekly exit-level re-tuning (ai_team.tuning): adopted only when unseen data agrees."""

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from ai_team import tuning
from ai_team.data import SyntheticMarketData
from ai_team.specialists.strategies import STOP_ATR, TAKE_PROFIT_ATR


def fake_backtests(monkeypatch, in_sample, oos):
    """in_sample/oos: {(sl, tp): (sharpe, return)}; anything missing scores badly."""

    def bt(df, pos, timeframe, sl=STOP_ATR, tp=TAKE_PROFIT_ATR, **_):
        table = oos if df.attrs.get("oos") else in_sample
        sharpe, ret = table.get((sl, tp), (-1.0, -0.01))
        return {"sharpe": sharpe, "total_return": ret, "trades": 10}

    monkeypatch.setattr(tuning, "backtest_with_stops", bt)


def frame():
    df = pd.DataFrame(
        {"close": range(100)}, index=pd.date_range("2026-01-01", periods=100, freq="4h", tz="UTC")
    )

    class Marking(pd.DataFrame):
        @property
        def iloc(self):  # mark the out-of-sample slice so the fake can tell the two apart
            outer = super().iloc

            class Wrap:
                def __getitem__(self, key):
                    part = outer[key]
                    part.attrs["oos"] = isinstance(key, slice) and key.start not in (None, 0)
                    return part

            return Wrap()

    return Marking(df)


def rule(df):
    return pd.Series(1.0, index=df.index)


@pytest.mark.parametrize(
    ("oos_candidate", "adopted"),
    [((1.0, 0.05), True), ((0.6, 0.05), False), ((1.0, -0.01), False)],
    ids=["clearly better", "within margin", "loses money"],
)
def test_candidate_needs_unseen_data_to_agree(monkeypatch, oos_candidate, adopted):
    fake_backtests(
        monkeypatch,
        in_sample={(3.0, 5.0): (2.0, 0.2), (STOP_ATR, TAKE_PROFIT_ATR): (0.5, 0.05)},
        oos={(3.0, 5.0): oos_candidate, (STOP_ATR, TAKE_PROFIT_ATR): (0.5, 0.02)},
    )
    r = tuning.tune_one(frame(), rule, "4h")
    assert r["candidate"] == [3.0, 5.0] and r["adopted"] is adopted
    assert (r["stop_atr"], r["take_profit_atr"]) == ((3.0, 5.0) if adopted else (STOP_ATR, TAKE_PROFIT_ATR))


def test_tuner_schedule_and_levels(tmp_path):
    t = tuning.Tuner(tmp_path)
    assert t.due("4h") and t.exit_levels("BTC/USDT", "4h") == {}
    now = datetime(2026, 2, 1, tzinfo=UTC)
    result = t.run(SyntheticMarketData(), ["BTC/USDT"], "4h", now=now)
    assert result["tested"] == len(tuning.TUNED) and result["errors"] == {}
    assert not t.due("4h", now + timedelta(days=6))
    assert t.due("4h", now + timedelta(days=7)) and t.due("1h", now)
    adopted = {n for n, v in result["params"]["BTC/USDT"].items() if v["adopted"]}
    assert set(t.exit_levels("BTC/USDT", "4h")) == adopted
    assert t.exit_levels("BTC/USDT", "1h") == {}  # levels tuned on other bars are not used


def test_strategist_and_backtest_use_tuned_levels():
    from ai_team.models import Task
    from ai_team.runtime import AgentContext, run_specialist

    params = {"exit_levels": {"trend_following": [3.0, 5.0]}}
    market = SyntheticMarketData(drift=0.002)
    ctx = AgentContext(Task("trend-following-strategist", "t", "BTC/USDT", "4h", params), market, {})
    r = run_specialist("trend-following-strategist", ctx)
    assert r.metrics["exit_levels_atr"] == [3.0, 5.0]
    for s in r.signals:
        if s.side == "long":
            a = r.metrics["atr"]
            assert s.stop_loss == pytest.approx(s.entry_price - 3.0 * a)
            assert s.take_profit == pytest.approx(s.entry_price + 5.0 * a)
    bt_ctx = AgentContext(
        Task(
            "backtesting-specialist",
            "t",
            "BTC/USDT",
            "4h",
            {**params, "strategies": ["trend_following", "swing"]},
        ),
        market,
        {},
    )
    res = run_specialist("backtesting-specialist", bt_ctx).metrics["results"]
    assert res["trend_following"]["exit_levels_atr"] == [3.0, 5.0]
    assert res["swing"]["exit_levels_atr"] == [STOP_ATR, TAKE_PROFIT_ATR]
