"""
Market data for the team.

``KrakenPublicData`` uses Kraken's public, key-less REST API (no credentials are
ever needed or accepted). ``SyntheticMarketData`` is a deterministic offline
source used by tests and as an explicit fallback.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Protocol

import numpy as np
import pandas as pd

from .history import CandleStore


log = logging.getLogger(__name__)

TIMEFRAME_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240, "1d": 1440}


def periods_per_year(timeframe: str) -> float:
    return 365 * 24 * 60 / TIMEFRAME_MINUTES[timeframe]


class MarketData(Protocol):
    name: str
    feeds: Any  # ai_team.feeds.PublicFeeds or SyntheticFeeds

    def ohlcv(self, pair: str, timeframe: str = "1h", limit: int = 500) -> pd.DataFrame: ...

    def order_book(self, pair: str, depth: int = 500) -> dict: ...

    def trades(self, pair: str, limit: int = 500) -> pd.DataFrame: ...


class KrakenUnavailable(RuntimeError):
    """Kraken could not be reached or reported a (possibly temporary) error."""


# Kraken spells some assets differently in pair names.
_KRAKEN_ASSET_ALIASES = {"XBT": "BTC", "XDG": "DOGE"}


class KrakenPublicData:
    name = "kraken-public"
    base = "https://api.kraken.com/0/public/"
    # Kraken's public API allows roughly one call per second; stay under it
    # across every thread that shares this process.
    MIN_INTERVAL = 0.35
    RETRIES = 3
    _throttle_lock = threading.Lock()
    _last_call = 0.0

    def __init__(
        self,
        timeout: float = 15.0,
        opener: urllib.request.OpenerDirector | None = None,
        history_dir: Path | None = None,
    ) -> None:
        self.timeout = timeout
        self._cache: dict[tuple, Any] = {}
        self._opener = opener or urllib.request.build_opener()
        # With a history directory, every download is merged into a local candle store,
        # so research sees more than Kraken's 720-candle window as the system keeps running.
        self.history = CandleStore(history_dir) if history_dir else None
        from .feeds import PublicFeeds

        self.feeds = PublicFeeds()

    @staticmethod
    def _symbol(pair: str) -> str:
        return pair.replace("/", "")

    @classmethod
    def _throttle(cls) -> None:
        with cls._throttle_lock:
            wait = cls.MIN_INTERVAL - (time.monotonic() - cls._last_call)
            if wait > 0:
                time.sleep(wait)
            cls._last_call = time.monotonic()

    def _get(self, endpoint: str, **params) -> dict:
        url = self.base + endpoint + "?" + urllib.parse.urlencode(params)
        last_error: Exception | None = None
        for attempt in range(self.RETRIES):
            self._throttle()
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "ai-team-paper/1.0"})
                with self._opener.open(req, timeout=self.timeout) as resp:
                    body = json.loads(resp.read())
            except (OSError, ValueError) as e:  # HTTP 5xx, timeouts, DNS, bad JSON
                last_error = e
            else:
                errors = body.get("error") or []
                if not errors:
                    return {k: v for k, v in body["result"].items() if k != "last"}
                # EService:* / EAPI:Rate limit are transient; anything else is a real error.
                if not any(e.startswith(("EService:", "EAPI:Rate limit")) for e in errors):
                    raise KrakenUnavailable(f"Kraken {endpoint} error: {errors}")
                last_error = KrakenUnavailable(f"Kraken {endpoint} error: {errors}")
            time.sleep(0.5 * 2**attempt)
        raise KrakenUnavailable(f"Kraken {endpoint} unavailable after {self.RETRIES} attempts: {last_error}")

    def _pair_names(self, pairs: list[str]) -> dict[str, str]:
        """Map Kraken's result keys (e.g. XBTUSDT) back to our pair names (BTC/USDT)."""
        key = ("pairmap", tuple(pairs))
        if key not in self._cache:
            info = self._get("AssetPairs", pair=",".join(self._symbol(p) for p in pairs))
            mapping = {}
            for k, v in info.items():
                base, quote = v["wsname"].split("/")
                name = f"{_KRAKEN_ASSET_ALIASES.get(base, base)}/{_KRAKEN_ASSET_ALIASES.get(quote, quote)}"
                mapping[k] = name
            self._cache[key] = mapping
        return self._cache[key]

    def ticker(self, pairs: list[str]) -> dict[str, dict[str, float]]:
        """Live top-of-book and 24h statistics; never cached."""
        names = self._pair_names(pairs)
        raw = self._get("Ticker", pair=",".join(self._symbol(p) for p in pairs))
        out = {}
        for k, t in raw.items():
            last, open_ = float(t["c"][0]), float(t["o"])
            out[names.get(k, k)] = {
                "last": last,
                "bid": float(t["b"][0]),
                "ask": float(t["a"][0]),
                "open_utc_day": open_,
                "change_pct_utc_day": (last / open_ - 1) * 100 if open_ else 0.0,
                "high_24h": float(t["h"][1]),
                "low_24h": float(t["l"][1]),
                "volume_24h": float(t["v"][1]),
                "vwap_24h": float(t["p"][1]),
                "trades_24h": int(t["t"][1]),
            }
        return out

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
            if self.history is not None:
                try:
                    df = self.history.merge(pair, timeframe, df, TIMEFRAME_MINUTES[timeframe])
                except (OSError, ValueError) as e:  # history is an optimisation, never a blocker
                    log.warning("candle history for %s %s not updated: %s", pair, timeframe, e)
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
        from .feeds import SyntheticFeeds

        self.feeds = SyntheticFeeds(self)

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

    def ticker(self, pairs: list[str]) -> dict[str, dict[str, float]]:
        out = {}
        for p in pairs:
            df = self.ohlcv(p, "1h", 25)
            last, open_ = float(df["close"].iloc[-1]), float(df["open"].iloc[0])
            out[p] = {
                "last": last,
                "bid": last * 0.9999,
                "ask": last * 1.0001,
                "open_utc_day": open_,
                "change_pct_utc_day": (last / open_ - 1) * 100,
                "high_24h": float(df["high"].max()),
                "low_24h": float(df["low"].min()),
                "volume_24h": float(df["volume"].sum()),
                "vwap_24h": float((df["close"] * df["volume"]).sum() / df["volume"].sum()),
                "trades_24h": 0,
            }
        return out

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
