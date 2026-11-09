"""
Market data for the team.

``KrakenPublicData`` uses Kraken's public, key-less REST API (no credentials are
ever needed or accepted). ``SyntheticMarketData`` is a deterministic offline
source used by tests and as an explicit fallback.
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from typing import Any, Protocol

import numpy as np
import pandas as pd


TIMEFRAME_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240, "1d": 1440}


def periods_per_year(timeframe: str) -> float:
    return 365 * 24 * 60 / TIMEFRAME_MINUTES[timeframe]


class MarketData(Protocol):
    name: str

    def ohlcv(self, pair: str, timeframe: str = "1h", limit: int = 500) -> pd.DataFrame: ...

    def order_book(self, pair: str, depth: int = 500) -> dict: ...

    def trades(self, pair: str, limit: int = 500) -> pd.DataFrame: ...


class KrakenPublicData:
    name = "kraken-public"
    base = "https://api.kraken.com/0/public/"

    def __init__(self, timeout: float = 15.0) -> None:
        self.timeout = timeout
        self._cache: dict[tuple, Any] = {}

    @staticmethod
    def _symbol(pair: str) -> str:
        return pair.replace("/", "")

    def _get(self, endpoint: str, **params) -> dict:
        url = self.base + endpoint + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": "ai-team-paper/1.0"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            body = json.loads(resp.read())
        if body.get("error"):
            raise RuntimeError(f"Kraken {endpoint} error: {body['error']}")
        return {k: v for k, v in body["result"].items() if k != "last"}

    def ohlcv(self, pair: str, timeframe: str = "1h", limit: int = 500) -> pd.DataFrame:
        key = ("ohlcv", pair, timeframe)
        if key not in self._cache:
            result = self._get("OHLC", pair=self._symbol(pair), interval=TIMEFRAME_MINUTES[timeframe])
            rows = next(iter(result.values()))
            df = pd.DataFrame(
                rows, columns=["time", "open", "high", "low", "close", "vwap", "volume", "count"]
            )
            df["date"] = pd.to_datetime(df["time"], unit="s", utc=True)
            df = df.set_index("date")[["open", "high", "low", "close", "volume"]].astype(float)
            self._cache[key] = df
        return self._cache[key].tail(limit)

    def order_book(self, pair: str, depth: int = 500) -> dict:
        key = ("book", pair)
        if key not in self._cache:
            book = next(iter(self._get("Depth", pair=self._symbol(pair), count=depth).values()))
            self._cache[key] = {
                "bids": [(float(p), float(q)) for p, q, _ in book["bids"]],
                "asks": [(float(p), float(q)) for p, q, _ in book["asks"]],
            }
        return self._cache[key]

    def trades(self, pair: str, limit: int = 500) -> pd.DataFrame:
        key = ("trades", pair)
        if key not in self._cache:
            rows = next(iter(self._get("Trades", pair=self._symbol(pair), count=limit).values()))
            df = pd.DataFrame(
                [(float(r[0]), float(r[1]), float(r[2]), r[3]) for r in rows],
                columns=["price", "amount", "time", "side"],
            )
            df["side"] = df["side"].map({"b": "buy", "s": "sell"})
            self._cache[key] = df
        return self._cache[key].tail(limit)


class SyntheticMarketData:
    """Deterministic random-walk market with trend/oscillation, for offline runs."""

    name = "synthetic"
    LENGTH = 2000

    BASE_PRICES = {
        "BTC/USDT": 75000.0,
        "ETH/USDT": 3500.0,
        "SOL/USDT": 180.0,
        "XRP/USDT": 0.6,
        "ADA/USDT": 0.45,
    }

    def __init__(self, seed: int = 7, drift: float = 0.0001) -> None:
        self.seed = seed
        self.drift = drift

    def _rng(self, pair: str, salt: int = 0) -> np.random.Generator:
        return np.random.default_rng(self.seed + sum(map(ord, pair)) + salt)

    def ohlcv(self, pair: str, timeframe: str = "1h", limit: int = 500) -> pd.DataFrame:
        rng = self._rng(pair)
        n = self.LENGTH  # fixed length so every caller sees the same latest price
        # Shared market factor so pairs are correlated, plus idiosyncratic noise.
        common = np.random.default_rng(self.seed).normal(self.drift, 0.006, n)
        idio = rng.normal(0, 0.004, n)
        wave = 0.04 * np.sin(np.arange(n) / 40)
        close = self.BASE_PRICES.get(pair, 100.0) * np.exp(np.cumsum(common + idio) + wave)
        spread = np.abs(rng.normal(0, 0.003, n))
        open_ = np.r_[close[0], close[:-1]]
        high = np.maximum(open_, close) * (1 + spread)
        low = np.minimum(open_, close) * (1 - spread)
        volume = rng.lognormal(3, 0.5, n) * (1 + 5 * np.abs(common))
        idx = pd.date_range(
            end=pd.Timestamp("2026-11-01", tz="UTC"),
            periods=n,
            freq=f"{TIMEFRAME_MINUTES[timeframe]}min",
        )
        df = pd.DataFrame(
            {"open": open_, "high": high, "low": low, "close": close, "volume": volume}, index=idx
        )
        return df.tail(limit)

    def order_book(self, pair: str, depth: int = 500) -> dict:
        rng = self._rng(pair, 1)
        mid = float(self.ohlcv(pair, "1h", 5)["close"].iloc[-1])
        tick = mid * 0.0001
        bids = [(mid - tick * (i + 1), float(rng.lognormal(0, 1))) for i in range(depth)]
        asks = [(mid + tick * (i + 1), float(rng.lognormal(0, 1))) for i in range(depth)]
        return {"bids": bids, "asks": asks}

    def trades(self, pair: str, limit: int = 500) -> pd.DataFrame:
        rng = self._rng(pair, 2)
        mid = float(self.ohlcv(pair, "1h", 5)["close"].iloc[-1])
        side = rng.choice(["buy", "sell"], size=limit, p=[0.55, 0.45])
        return pd.DataFrame(
            {
                "price": mid * (1 + rng.normal(0, 0.0005, limit)),
                "amount": rng.lognormal(-2, 1, limit),
                "time": time.time() - np.arange(limit)[::-1],
                "side": side,
            }
        )
