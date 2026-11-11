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
