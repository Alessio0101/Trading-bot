"""Plain pandas/numpy indicators so the team runs without TA-Lib."""

from __future__ import annotations

import numpy as np
import pandas as pd


def ema(s: pd.Series, period: int) -> pd.Series:
    return s.ewm(span=period, adjust=False).mean()


def sma(s: pd.Series, period: int) -> pd.Series:
    return s.rolling(period).mean()


def rsi(s: pd.Series, period: int = 14) -> pd.Series:
    delta = s.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(100.0)


def macd(s: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    line = ema(s, fast) - ema(s, slow)
    sig = ema(line, signal)
    return pd.DataFrame({"macd": line, "signal": sig, "hist": line - sig})


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    prev = df["close"].shift()
    tr = pd.concat([df["high"] - df["low"], (df["high"] - prev).abs(), (df["low"] - prev).abs()], axis=1).max(
        axis=1
    )
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def bollinger(s: pd.Series, period: int = 20, width: float = 2.0) -> pd.DataFrame:
    mid = sma(s, period)
    std = s.rolling(period).std()
    return pd.DataFrame({"mid": mid, "upper": mid + width * std, "lower": mid - width * std})


def donchian(df: pd.DataFrame, period: int = 20) -> pd.DataFrame:
    return pd.DataFrame({"upper": df["high"].rolling(period).max(), "lower": df["low"].rolling(period).min()})


def obv(df: pd.DataFrame) -> pd.Series:
    direction = np.sign(df["close"].diff()).fillna(0)
    return (direction * df["volume"]).cumsum()


def log_returns(s: pd.Series) -> pd.Series:
    return np.log(s / s.shift()).dropna()


def realised_vol(s: pd.Series, periods_per_year: float) -> float:
    r = log_returns(s)
    return float(r.std() * np.sqrt(periods_per_year)) if len(r) > 1 else 0.0


def max_drawdown(equity: pd.Series) -> float:
    peak = equity.cummax()
    return float(((equity - peak) / peak).min()) if len(equity) else 0.0


def sharpe(returns: pd.Series, periods_per_year: float) -> float:
    if len(returns) < 2 or returns.std() == 0:
        return 0.0
    return float(returns.mean() / returns.std() * np.sqrt(periods_per_year))
