"""Local candle history (ai_team.history) and learning from the team's own signals."""

import pandas as pd
import pytest

from ai_team.data import KrakenPublicData
from ai_team.history import MAX_ROWS, CandleStore, last_contiguous
from ai_team.tests.test_operations import FakeOpener


H4 = pd.Timedelta(hours=4)


def candles(start: str, n: int, base: float = 100.0) -> pd.DataFrame:
    idx = pd.date_range(start, periods=n, freq="4h", tz="UTC", name="date")
    close = [base + i for i in range(n)]
    return pd.DataFrame(
        {"open": close, "high": close, "low": close, "close": close, "volume": 1.0}, index=idx
    )


class TestCandleStore:
    def test_merge_grows_history_and_fresh_values_win(self, tmp_path):
        store = CandleStore(tmp_path)
        first = candles("2026-01-01", 10)
        store.merge("BTC/USDT", "4h", first, 240)
        second = candles("2026-01-02 00:00", 10, base=500.0)  # overlaps the last 4 candles of the first
        merged = store.merge("BTC/USDT", "4h", second, 240)
        assert len(merged) == 16
        assert merged.index.is_monotonic_increasing and not merged.index.duplicated().any()
        assert merged.loc[second.index[0], "close"] == 500.0  # the newer download replaced the old candle
        assert merged["close"].iloc[0] == 100.0
        reloaded = store.load("BTC/USDT", "4h")
        pd.testing.assert_frame_equal(reloaded, merged, check_freq=False)
        assert (tmp_path / "BTC-USDT_4h.csv").exists()
        assert list(tmp_path.glob("*.tmp")) == []

    def test_history_before_a_long_gap_is_dropped(self, tmp_path):
        store = CandleStore(tmp_path)
        store.merge("ETH/USDT", "4h", candles("2026-01-01", 20), 240)
        later = candles("2026-03-01", 5)  # the computer was off for two months
        merged = store.merge("ETH/USDT", "4h", later, 240)
        assert len(merged) == 5 and merged.index[0] == later.index[0]

    @pytest.mark.parametrize(("hole_candles", "kept"), [(0, 10), (3, 10), (4, 5)])
    def test_last_contiguous_threshold(self, hole_candles, kept):
        a = candles("2026-01-01", 5)
        b = candles(str(a.index[-1] + H4 * (1 + hole_candles)), 5)
        assert len(last_contiguous(pd.concat([a, b]), H4)) == kept

    def test_last_contiguous_empty_and_single(self):
        assert len(last_contiguous(candles("2026-01-01", 0), H4)) == 0
        assert len(last_contiguous(candles("2026-01-01", 1), H4)) == 1

    def test_rows_are_capped(self, tmp_path, monkeypatch):
        from ai_team import history

        monkeypatch.setattr(history, "MAX_ROWS", 8)
        merged = CandleStore(tmp_path).merge("BTC/USDT", "4h", candles("2026-01-01", 20), 240)
        assert len(merged) == 8 and merged["close"].iloc[-1] == 119.0
        assert MAX_ROWS > 3000  # the store holds at least what research asks for

    def test_corrupt_file_is_treated_as_empty(self, tmp_path):
        (tmp_path / "BTC-USDT_4h.csv").write_text("not,a,csv\n\x00")
        store = CandleStore(tmp_path)
        assert len(store.load("BTC/USDT", "4h")) == 0
        assert len(store.merge("BTC/USDT", "4h", candles("2026-01-01", 3), 240)) == 3

    def test_kraken_client_merges_into_store(self, tmp_path):
        def ohlc(start, n):
            t0 = int(pd.Timestamp(start, tz="UTC").timestamp())
            rows = [[t0 + i * 14400, "1", "1", "1", str(100 + i), "1", "2", 3] for i in range(n)]
            return {"error": [], "result": {"XXBTZUSD": rows, "last": 0}}

        KrakenPublicData(opener=FakeOpener([ohlc("2026-01-01", 6)]), history_dir=tmp_path).ohlcv(
            "BTC/USDT", "4h"
        )
        df = KrakenPublicData(opener=FakeOpener([ohlc("2026-01-01 16:00", 6)]), history_dir=tmp_path).ohlcv(
            "BTC/USDT", "4h", limit=3000
        )
        assert len(df) == 10  # 6 + 6 with 2 overlapping candles
        no_history = KrakenPublicData(opener=FakeOpener([ohlc("2026-01-01 16:00", 6)]))
        assert len(no_history.ohlcv("BTC/USDT", "4h", limit=3000)) == 6


# ---------------- learning from the team's own signals ----------------

from ai_team.models import Report, Signal, Task  # noqa: E402
from ai_team.runtime import AgentContext, run_specialist  # noqa: E402
from ai_team.track_record import (  # noqa: E402
    HORIZON_BARS,
    ROUND_TRIP_COST,
    WEIGHT_MAX,
    WEIGHT_MIN,
    SignalLedger,
    weight_from,
)


class FrozenMarket:
    """Stand-in market with fixed 4-hour candles."""

    name = "test"

    def __init__(self, closes, start="2026-01-01"):
        self.df = candles(start, len(closes))
        self.df["close"] = closes

    def upto(self, n):
        m = FrozenMarket([0.0])
        m.df = self.df.iloc[:n]
        return m

    def ohlcv(self, pair, timeframe="4h", limit=500):
        return self.df.tail(limit)


def report(agent, side="long", pair="BTC/USDT", conf=0.6, strategy="momentum"):
    return Report(agent, "t", "ok", "s", signals=[Signal(pair, side, conf, agent, "test", strategy=strategy)])


class TestWeights:
    @pytest.mark.parametrize(
        ("wins", "n", "expected"),
        [
            (0, 0, 1.0),
            (5, 10, 1.0),
            (10, 10, 1.25),
            (0, 10, 0.75),
            (1, 1, 1.045),
            (100, 100, 1.25),
            (0, 100, 0.75),
        ],
    )
    def test_weight_is_shrunk_and_bounded(self, wins, n, expected):
        w = weight_from(wins, n)
        assert w == pytest.approx(expected, abs=1e-3)
        assert WEIGHT_MIN <= w <= WEIGHT_MAX


class TestSignalLedger:
    def test_records_once_per_candle_and_only_longs(self, tmp_path):
        ledger = SignalLedger(tmp_path / "l.json")
        m = FrozenMarket([100.0] * 10)
        inbox = {"a": report("a"), "b": report("b", side="exit"), "c": report("c", pair="ETH/USDT")}
        assert ledger.record(inbox, "BTC/USDT", "4h", m) == 1
        assert ledger.record(inbox, "BTC/USDT", "4h", m) == 0  # same candle: not counted twice
        assert ledger.record(inbox, "BTC/USDT", "4h", m.upto(5)) == 1  # a different candle is new
        assert ledger.record({}, "BTC/USDT", "4h", m) == 0
        assert ledger.pending() == 2

    def test_evaluates_after_the_horizon_net_of_costs(self, tmp_path):
        ledger = SignalLedger(tmp_path / "l.json")
        closes = [100.0] * 5 + [110.0] * 20  # rises 10 % after the signal candle
        m = FrozenMarket(closes)
        ledger.record({"win": report("win")}, "BTC/USDT", "4h", m.upto(5))  # signal at close 100
        signal_candle = m.df.index[4]
        too_early = signal_candle + H4 * HORIZON_BARS
        assert ledger.evaluate(m, now=too_early) == 0  # target candle still forming
        assert ledger.evaluate(m, now=too_early + H4) == 1
        s = ledger.scores()["win"]
        assert s["signals"] == 1 and s["wins"] == 1
        assert s["avg_net_return"] == pytest.approx(0.10 - ROUND_TRIP_COST, abs=1e-6)
        assert ledger.evaluate(m, now=too_early + H4 * 5) == 0  # already scored

    def test_flat_market_is_a_loss_after_costs(self, tmp_path):
        ledger = SignalLedger(tmp_path / "l.json")
        m = FrozenMarket([100.0] * 30)
        ledger.record({"flat": report("flat")}, "BTC/USDT", "4h", m.upto(5))
        ledger.evaluate(m, now=m.df.index[-1] + H4)
        s = ledger.scores()["flat"]
        assert s["wins"] == 0 and s["avg_net_return"] == pytest.approx(-ROUND_TRIP_COST)

    def test_missing_data_waits(self, tmp_path):
        ledger = SignalLedger(tmp_path / "l.json")
        m = FrozenMarket([100.0] * 6)
        ledger.record({"a": report("a")}, "BTC/USDT", "4h", m)
        assert ledger.evaluate(m.upto(6), now=m.df.index[-1] + H4 * 30) == 0  # no candle near the target
        assert ledger.pending() == 1

    def test_corrupt_ledger_starts_empty(self, tmp_path):
        (tmp_path / "l.json").write_text("{nope")
        ledger = SignalLedger(tmp_path / "l.json")
        assert ledger.scores() == {} and ledger.pending() == 0


class TestAggregationUsesTrackRecord:
    def run_agg(self, weights):
        inbox = {
            "good": report("good", conf=0.6, strategy="momentum"),
            "bad": report("bad", conf=0.6, strategy="momentum"),
        }
        ctx = AgentContext(
            Task("signal-aggregation-specialist", "t", "BTC/USDT", "4h", {"agent_weights": weights}),
            FrozenMarket([100.0] * 5),
            inbox,
        )
        return run_specialist("signal-aggregation-specialist", ctx)

    def test_bad_record_lowers_confidence(self):
        neutral = self.run_agg({}).metrics["proposals"][0]["confidence"]
        lowered = self.run_agg({"good": 0.75, "bad": 0.75}).metrics["proposals"][0]["confidence"]
        assert lowered < neutral

    def test_good_record_cannot_exceed_reported_conviction(self):
        r = self.run_agg({"good": 1.25, "bad": 1.25})
        assert r.metrics["proposals"][0]["confidence"] <= 0.6 + 1e-9
        assert r.metrics["learned_weights"] == {"good": 1.25, "bad": 1.25}


def test_coordinator_cycle_records_and_scores_signals(tmp_path):
    from ai_team.coordinator import Coordinator
    from ai_team.data import SyntheticMarketData
    from ai_team.state import StateStore

    class DownGateway:
        def health(self):
            return {"ok": False}

        def portfolio(self):
            return {}

        def submit(self, proposal):
            return {"approved": False, "reasons": ["gateway unreachable"]}

    state = StateStore(tmp_path)
    coord = Coordinator(SyntheticMarketData(seed=11, drift=0.0012), DownGateway(), state)
    coord.cycle(["BTC/USDT"], "1h", submit=False)
    learning = [d for d in state.decisions() if d.get("type") == "learning"]
    assert learning and "track_record" in learning[-1]
    assert (tmp_path / "signal_ledger.json").exists() or coord.ledger.pending() == 0


# ---------------- stop-aware backtest ----------------

from ai_team.specialists.quant import FEE, SLIPPAGE, backtest_with_stops  # noqa: E402


def bars(rows):
    """rows: (open, high, low, close); ATR(14) of these frames is driven by the ranges."""
    idx = pd.date_range("2026-01-01", periods=len(rows), freq="4h", tz="UTC")
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx).assign(volume=1.0)


FLAT = [(100, 101, 99, 100)] * 20  # ATR settles at 2 -> stop 96, target 106 for an entry at 100
COST = FEE + SLIPPAGE


class TestBacktestWithStops:
    def run(self, tail, want):
        df = bars(FLAT + tail)
        pos = pd.Series([0.0] * 19 + want, index=df.index)
        return backtest_with_stops(df, pos, "4h")

    def test_take_profit(self):
        m = self.run([(100, 101, 99, 100), (101, 107, 100, 105)], [1.0, 1.0, 0.0])
        assert m["trades"] == 1 and m["take_profit_exits"] == 1 and m["win_rate"] == 1.0
        assert m["total_return"] == pytest.approx(106 / 100 * (1 - COST) ** 2 - 1, abs=2e-3)

    def test_stop_loss_and_gap_fills_at_open(self):
        m = self.run([(100, 101, 99, 100), (100, 100, 95, 97)], [1.0, 1.0, 1.0])
        assert m["stop_exits"] == 1 and m["win_rate"] == 0.0
        gap = self.run([(100, 101, 99, 100), (90, 91, 89, 90)], [1.0, 1.0, 0.0])
        assert gap["stop_exits"] == 1
        assert gap["total_return"] == pytest.approx(90 / 100 * (1 - COST) ** 2 - 1, abs=2e-3)

    def test_candle_touching_both_levels_counts_as_stop(self):
        m = self.run([(100, 101, 99, 100), (100, 110, 90, 100)], [1.0, 1.0, 1.0])
        assert m["stop_exits"] == 1 and m["take_profit_exits"] == 0

    def test_rule_exit_at_close_and_re_entry_after_stop(self):
        m = self.run([(100, 101, 99, 100), (100, 101, 99, 101)], [1.0, 1.0, 0.0])
        assert m["rule_exits"] == 1 and m["stop_exits"] == 0
        again = self.run([(100, 101, 99, 100), (100, 100, 95, 97), (97, 98, 96, 97)], [1.0, 1.0, 1.0, 1.0])
        assert again["stop_exits"] == 1 and again["trades"] == 2  # the rule is still long: re-enters

    def test_never_long_is_flat(self):
        m = self.run([(100, 101, 99, 100)], [0.0, 0.0])
        assert m["trades"] == 0 and m["total_return"] == 0 and m["win_rate"] is None
