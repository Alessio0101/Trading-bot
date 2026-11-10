"""
Free, key-less public data feeds for the agents that previously had no data.

* news         - RSS from CoinDesk, Cointelegraph and Decrypt
* fundamentals - CoinGecko /coins/markets (market cap, supply, volume, ATH)
* global       - CoinGecko /global (total crypto market cap, dominance)
* second venue - Coinbase Exchange public tickers (for cross-venue comparison)
* options      - Deribit public option book summaries (BTC, ETH)

No feed needs an account or credentials. Headlines and other text from these
feeds are untrusted data: they are only scored and displayed, never executed or
followed as instructions. ``SyntheticFeeds`` provides deterministic offline data
for tests and for ``--data synthetic`` runs.
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any

import numpy as np


MAX_BYTES = 3_000_000  # refuse oversized responses
USER_AGENT = "ai-team-paper/1.0"

NEWS_FEEDS = {
    "CoinDesk": "https://www.coindesk.com/arc/outboundfeeds/rss",
    "Cointelegraph": "https://cointelegraph.com/rss",
    "Decrypt": "https://decrypt.co/feed",
}
COINGECKO_IDS = {"BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana", "XRP": "ripple", "ADA": "cardano"}
ASSET_NAMES = {
    "BTC": ["bitcoin", "btc"],
    "ETH": ["ethereum", "ether", "eth"],
    "SOL": ["solana", "sol"],
    "XRP": ["xrp", "ripple"],
    "ADA": ["cardano", "ada"],
}
OPTION_ASSETS = {"BTC", "ETH"}


class FeedUnavailable(RuntimeError):
    """A public feed could not be reached or returned unusable data."""


class PublicFeeds:
    # Small per-process cache so a coordinator cycle (5 pairs x several agents)
    # makes one request per feed, and repeated cycles respect free rate limits.
    TTL_S = 240
    _cache: dict[str, tuple[float, Any]] = {}
    _lock = threading.Lock()

    def __init__(self, opener: urllib.request.OpenerDirector | None = None, timeout: float = 20.0) -> None:
        self._opener = opener or urllib.request.build_opener()
        self.timeout = timeout

    def _fetch(self, url: str, retries: int = 2) -> bytes:
        last: Exception | None = None
        for attempt in range(retries):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
                with self._opener.open(req, timeout=self.timeout) as r:
                    data = r.read(MAX_BYTES + 1)
                if len(data) > MAX_BYTES:
                    raise FeedUnavailable(f"response from {url} is larger than {MAX_BYTES} bytes")
                return data
            except (OSError, ValueError) as e:
                last = e
                time.sleep(0.5 * (attempt + 1))
        raise FeedUnavailable(f"{url} unavailable: {last}")

    def _cached(self, key: str, fn):
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(key)
            if hit and now - hit[0] < self.TTL_S:
                return hit[1]
        value = fn()
        with self._lock:
            self._cache[key] = (time.monotonic(), value)
        return value

    def _json(self, url: str) -> Any:
        try:
            return json.loads(self._fetch(url))
        except ValueError as e:
            raise FeedUnavailable(f"{url} returned invalid JSON: {e}") from e

    # ---------------- news ----------------
    def _rss(self, source: str, url: str) -> list[dict[str, Any]]:
        try:
            # ElementTree never resolves external entities, the bundled expat (>= 2.4.1) limits
            # entity expansion, and _fetch caps the size, so untrusted RSS is parsed safely.
            root = ET.fromstring(self._fetch(url))  # noqa: S314
        except ET.ParseError as e:
            raise FeedUnavailable(f"{source} RSS could not be parsed: {e}") from e
        items = []
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            if not title:
                continue
            published = item.findtext("pubDate")
            try:
                ts = parsedate_to_datetime(published).isoformat() if published else None
            except (TypeError, ValueError):
                ts = None
            items.append(
                {
                    "headline": re.sub(r"\s+", " ", title)[:300],
                    "source": source,
                    "published": ts,
                    "link": (item.findtext("link") or "").strip()[:500],
                }
            )
        return items

    def all_news(self) -> dict[str, Any]:
        def load():
            items, errors = [], {}
            for source, url in NEWS_FEEDS.items():
                try:
                    items.extend(self._rss(source, url))
                except FeedUnavailable as e:
                    errors[source] = str(e)
            if not items:
                raise FeedUnavailable(f"no news feed reachable: {errors}")
            return {"items": items, "errors": errors}

        return self._cached("news", load)

    def news(self, asset: str, limit: int = 15) -> dict[str, Any]:
        """Headlines mentioning the asset; falls back to general crypto headlines."""
        data = self.all_news()
        names = ASSET_NAMES.get(asset, [asset.lower()])
        pattern = re.compile(r"\b(" + "|".join(map(re.escape, names)) + r")\b", re.IGNORECASE)
        matched = [i for i in data["items"] if pattern.search(i["headline"])]
        chosen = matched if matched else data["items"]
        chosen = sorted(chosen, key=lambda i: i["published"] or "", reverse=True)[:limit]
        return {
            "items": chosen,
            "asset_specific": bool(matched),
            "errors": data["errors"],
            "sources": sorted({i["source"] for i in data["items"]}),
        }

    # ---------------- fundamentals ----------------
    def fundamentals(self, assets: list[str]) -> dict[str, dict[str, Any]]:
        ids = [COINGECKO_IDS[a] for a in assets if a in COINGECKO_IDS]
        if not ids:
            raise FeedUnavailable(f"no CoinGecko id for {assets}")

        def load():
            url = (
                "https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&price_change_percentage=7d,30d"
                "&ids=" + ",".join(sorted(COINGECKO_IDS.values()))
            )
            rows = self._json(url)
            if not isinstance(rows, list):
                raise FeedUnavailable(f"CoinGecko markets returned {type(rows).__name__}")
            return {r["id"]: r for r in rows}

        by_id = self._cached("coingecko-markets", load)
        inverse = {v: k for k, v in COINGECKO_IDS.items()}
        return {inverse[i]: by_id[i] for i in ids if i in by_id}

    def global_market(self) -> dict[str, Any]:
        def load():
            data = self._json("https://api.coingecko.com/api/v3/global")
            if "data" not in data:
                raise FeedUnavailable("CoinGecko /global returned no data")
            return data["data"]

        return self._cached("coingecko-global", load)

    # ---------------- second venue ----------------
    def second_venue(self, pairs: list[str]) -> dict[str, dict[str, Any]]:
        out = {}
        for pair in pairs:
            pid = pair.replace("/", "-")

            def load(pid=pid):
                t = self._json(f"https://api.exchange.coinbase.com/products/{pid}/ticker")
                if "price" not in t:
                    raise FeedUnavailable(f"Coinbase {pid}: {t.get('message', 'no price')}")
                return {
                    "last": float(t["price"]),
                    "bid": float(t["bid"]),
                    "ask": float(t["ask"]),
                    "time": t.get("time"),
                }

            try:
                out[pair] = self._cached(f"coinbase-{pid}", load)
            except FeedUnavailable:
                continue
        if not out:
            raise FeedUnavailable(f"Coinbase has no ticker for {pairs}")
        return out

    # ---------------- options ----------------
    def options(self, asset: str) -> list[dict[str, Any]]:
        if asset not in OPTION_ASSETS:
            raise FeedUnavailable(f"no listed options for {asset} on Deribit (only {sorted(OPTION_ASSETS)})")

        def load():
            url = f"https://www.deribit.com/api/v2/public/get_book_summary_by_currency?currency={asset}&kind=option"
            data = self._json(url)
            if "result" not in data:
                raise FeedUnavailable(f"Deribit returned no result for {asset}")
            return data["result"]

        return self._cached(f"deribit-{asset}", load)


class SyntheticFeeds:
    """Deterministic offline stand-ins with the same shapes as PublicFeeds."""

    HEADLINES = [
        "Bitcoin ETF inflow hits record as institutional adoption grows",
        "Ethereum upgrade launch boosts network activity",
        "Regulator lawsuit weighs on crypto exchange shares",
        "Solana partnership with payments firm announced",
        "XRP rally continues after court approval",
        "Cardano developers launch new scaling upgrade",
    ]

    def __init__(self, market) -> None:
        self.market = market

    def news(self, asset: str, limit: int = 15) -> dict[str, Any]:
        names = ASSET_NAMES.get(asset, [asset.lower()])
        items = [
            {"headline": h, "source": "synthetic", "published": "2026-11-01T00:00:00+00:00", "link": ""}
            for h in self.HEADLINES
            if any(n in h.lower() for n in names)
        ]
        return {
            "items": items[:limit]
            or [{"headline": self.HEADLINES[0], "source": "synthetic", "published": None, "link": ""}],
            "asset_specific": bool(items),
            "errors": {},
            "sources": ["synthetic"],
        }

    def fundamentals(self, assets: list[str]) -> dict[str, dict[str, Any]]:
        out = {}
        for a in assets:
            price = float(self.market.ohlcv(f"{a}/USDT", "1h", 5)["close"].iloc[-1])
            supply = {"BTC": 19.9e6, "ETH": 120e6, "SOL": 480e6, "XRP": 58e9, "ADA": 36e9}.get(a, 1e9)
            out[a] = {
                "market_cap": price * supply,
                "fully_diluted_valuation": price * supply * 1.1,
                "circulating_supply": supply,
                "total_supply": supply * 1.05,
                "max_supply": supply * 1.1,
                "total_volume": price * supply * 0.03,
                "ath": price * 1.5,
                "ath_change_percentage": -33.3,
                "price_change_percentage_7d_in_currency": 1.2,
                "price_change_percentage_30d_in_currency": -4.0,
                "market_cap_rank": 1,
            }
        return out

    def global_market(self) -> dict[str, Any]:
        return {
            "total_market_cap": {"usd": 2.5e12},
            "market_cap_change_percentage_24h_usd": 1.1,
            "market_cap_percentage": {"btc": 57.0, "eth": 12.0},
            "active_cryptocurrencies": 15000,
        }

    def second_venue(self, pairs: list[str]) -> dict[str, dict[str, Any]]:
        out = {}
        for p in pairs:
            last = float(self.market.ohlcv(p, "1h", 5)["close"].iloc[-1]) * 1.0004
            out[p] = {"last": last, "bid": last * 0.9999, "ask": last * 1.0001, "time": None}
        return out

    def options(self, asset: str) -> list[dict[str, Any]]:
        if asset not in OPTION_ASSETS:
            raise FeedUnavailable(f"no listed options for {asset} on Deribit (only {sorted(OPTION_ASSETS)})")
        under = float(self.market.ohlcv(f"{asset}/USDT", "1h", 5)["close"].iloc[-1])
        rows = []
        rng = np.random.default_rng(3)
        today = datetime.now(UTC).date()
        for days in (7, 30):
            code = (today + timedelta(days=days)).strftime("%d%b%y").upper()
            for k in (0.8, 0.9, 1.0, 1.1, 1.2):
                for cp in ("C", "P"):
                    rows.append(
                        {
                            "instrument_name": f"{asset}-{code}-{int(under * k)}-{cp}",
                            "underlying_price": under,
                            "mark_iv": 50 + 10 * abs(1 - k) * 10 + days / 10,
                            "open_interest": float(rng.integers(50, 500)),
                            "days": days,
                        }
                    )
        return rows
