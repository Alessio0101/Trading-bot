"""
Local candle history, so research gets longer every day the system runs.

Kraken's public OHLC endpoint returns only the newest 720 candles (about four months
of 4-hour bars). ``CandleStore`` keeps every candle it has seen in a CSV per pair and
timeframe under ``state/history`` and merges each fresh download into it. Backtests,
the walk-forward ML model and the RL simulator then see more history over time.

Only one contiguous stretch is used: if the computer was off for longer than Kraken's
window, the stored candles before the gap are dropped rather than joined across it.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pandas as pd

from .fsutil import atomic_write_text


MAX_ROWS = 6000  # per pair and timeframe; 4h bars = ~2.7 years, 1m bars = ~4 days
GAP_FACTOR = 3  # more than 3 missing candles in a row starts a new stretch
COLUMNS = ["open", "high", "low", "close", "volume"]


def last_contiguous(df: pd.DataFrame, interval: pd.Timedelta) -> pd.DataFrame:
    """The newest stretch of candles without a hole longer than GAP_FACTOR intervals."""
    if len(df) < 2:
        return df
    gaps = df.index.to_series().diff() > interval * (GAP_FACTOR + 1)
    if not gaps.any():
        return df
    start = gaps[gaps].index[-1]
    return df.loc[start:]


class CandleStore:
    _lock = threading.Lock()

    def __init__(self, directory: Path) -> None:
        self.dir = Path(directory)

    def _path(self, pair: str, timeframe: str) -> Path:
        return self.dir / f"{pair.replace('/', '-')}_{timeframe}.csv"

    def load(self, pair: str, timeframe: str) -> pd.DataFrame:
        path = self._path(pair, timeframe)
        if not path.exists():
            return pd.DataFrame(columns=COLUMNS, index=pd.DatetimeIndex([], tz="UTC", name="date"))
        try:
            df = pd.read_csv(path, index_col="date", parse_dates=["date"])
        except (OSError, ValueError, KeyError):
            return pd.DataFrame(columns=COLUMNS, index=pd.DatetimeIndex([], tz="UTC", name="date"))
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        return df[COLUMNS].astype(float)

    def merge(self, pair: str, timeframe: str, fresh: pd.DataFrame, interval_minutes: int) -> pd.DataFrame:
        """Merge freshly downloaded candles into the store and return the usable history.

        Fresh values win for overlapping timestamps: the newest candle is still forming
        and Kraken's latest copy of it is the right one.
        """
        interval = pd.Timedelta(minutes=interval_minutes)
        with self._lock:
            old = self.load(pair, timeframe)
            parts = [old, fresh[COLUMNS]] if len(old) else [fresh[COLUMNS]]
            merged = pd.concat(parts)
            merged = merged[~merged.index.duplicated(keep="last")].sort_index()
            merged = last_contiguous(merged, interval).tail(MAX_ROWS)
            self.dir.mkdir(parents=True, exist_ok=True)
            out = merged.copy()
            out.index.name = "date"
            atomic_write_text(self._path(pair, timeframe), out.to_csv())
        return merged
