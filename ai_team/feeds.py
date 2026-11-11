"""
Free, key-less public data feeds for the agents that previously had no data.

* news         - RSS from CoinDesk, Cointelegraph and Decrypt
* fundamentals - CoinGecko /coins/markets (market cap, supply, volume, ATH)
* global       - CoinGecko /global (total crypto market cap, dominance)
* second venue - Coinbase Exchange public tickers (for cross-venue comparison)
* options      - Deribit public option book summaries (BTC, ETH)
* economic     - FRED: Fed funds rate, CPI, 10-year Treasury yield, broad dollar index
                 (needs a free FRED API key saved with ``python -m ai_team.keys setup``)
* headlines    - optional Claude scoring of headlines (ai_team.llm_news), with a saved key
* fear & greed - alternative.me Crypto Fear & Greed Index
* derivatives  - Kraken Futures perpetuals: funding rate and open interest
* reddit       - today's top posts in r/CryptoCurrency and r/Bitcoin (titles only)
* podcasts     - latest episode titles and show notes of well-known crypto podcasts,
                 found through the Apple Podcasts search API (no audio is downloaded)

Only FRED and Claude need a key, and both are optional. Headlines and other text from these
feeds are untrusted data: they are only scored and displayed, never executed or
followed as instructions. ``SyntheticFeeds`` provides deterministic offline data
for tests and for ``--data synthetic`` runs.
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any

import numpy as np

from . import local_config


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
FUTURES_SYMBOLS = {
    "BTC": "PF_XBTUSD",
    "ETH": "PF_ETHUSD",
    "SOL": "PF_SOLUSD",
    "XRP": "PF_XRPUSD",
    "ADA": "PF_ADAUSD",
}
SUBREDDITS = ["CryptoCurrency", "Bitcoin"]
PODCASTS = ["Bankless", "Unchained Laura Shin", "The Pomp Podcast", "What Bitcoin Did", "Coin Bureau Podcast"]


class FeedUnavailable(RuntimeError):
    """A public feed could not be reached or returned unusable data."""


class PublicFeeds:
    # Small per-process cache so a coordinator cycle (5 pairs x several agents)
    # makes one request per feed, and repeated cycles respect free rate limits.
    TTL_S = 240
    _cache: dict[str, tuple[float, Any]] = {}
    _lock = threading.Lock()

    def __init__(
        self,
        opener: urllib.request.OpenerDirector | None = None,
        timeout: float = 20.0,
        scorer: Any = None,
        scorers: dict[str, Any] | None = None,
    ) -> None:
        self._opener = opener or urllib.request.build_opener()
        self.timeout = timeout
        # ai_team.llm_news.ClaudeNewsScorer per text kind ("news", "reddit", "podcasts"),
        # each with its own cache and hourly budget; only used when a key is configured.
        self.scorers = dict(scorers or {})
        if scorer is not None:
            self.scorers.setdefault("news", scorer)

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

    FAILURE_TTL_S = 600  # a feed that just failed is not retried for this long

    def _cached(self, key: str, fn, ttl: float | None = None):
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(key)
            if hit and isinstance(hit[1], FeedUnavailable):
                if now - hit[0] < self.FAILURE_TTL_S:
                    raise hit[1]  # do not hammer a feed that is down (once per pair per cycle)
            elif hit and now - hit[0] < (ttl or self.TTL_S):
                return hit[1]
        try:
            value = fn()
        except FeedUnavailable as e:
            with self._lock:
                self._cache[key] = (time.monotonic(), e)
            raise
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

    # ---------------- headline scoring (Claude) ----------------
    def headline_scores(self, headlines: list[str], kind: str = "news") -> dict[str, dict[str, Any]]:
        scorer = self.scorers.get(kind)
        if scorer is None or not scorer.enabled:
            return {}
        return scorer.scores(headlines)

    # ---------------- crowd and positioning ----------------
    def fear_greed(self) -> dict[str, Any]:
        def load():
            data = self._json("https://api.alternative.me/fng/?limit=8")
            rows = data.get("data") or []
            if not rows:
                raise FeedUnavailable("Fear & Greed index returned no data")
            values = [int(r["value"]) for r in rows]
            return {"value": values[0], "label": rows[0].get("value_classification"), "week_ago": values[-1]}

        return self._cached("fear-greed", load, ttl=3600)

    def derivatives(self) -> dict[str, dict[str, Any]]:
        """Funding and open interest of Kraken's perpetual futures, by asset."""

        def load():
            data = self._json("https://futures.kraken.com/derivatives/api/v3/tickers")
            by_symbol = {t.get("symbol"): t for t in data.get("tickers") or []}
            out = {}
            for asset, symbol in FUTURES_SYMBOLS.items():
                t = by_symbol.get(symbol)
                if not t or not t.get("markPrice"):
                    continue
                mark = float(t["markPrice"])
                # Kraken reports the hourly funding rate in USD per contract; relative to the
                # mark price and annualised it reads like a yearly cost of holding a long.
                hourly = float(t.get("fundingRate") or 0.0) / mark
                out[asset] = {
                    "funding_annualised": hourly * 24 * 365,
                    "open_interest": float(t.get("openInterest") or 0.0),
                    "mark_price": mark,
                }
            if not out:
                raise FeedUnavailable("Kraken Futures returned no perpetual tickers")
            return out

        return self._cached("kraken-futures", load, ttl=900)

    def reddit(self) -> dict[str, Any]:
        def load():
            posts, errors = [], {}
            for sub in SUBREDDITS:
                try:
                    data = self._json(f"https://www.reddit.com/r/{sub}/top.json?t=day&limit=25")
                    for child in (data.get("data") or {}).get("children") or []:
                        p = child.get("data") or {}
                        title = re.sub(r"\s+", " ", (p.get("title") or "")).strip()[:300]
                        if title and not p.get("stickied"):
                            posts.append(
                                {"title": title, "score": int(p.get("score") or 0), "subreddit": sub}
                            )
                except FeedUnavailable as e:
                    errors[sub] = str(e)
            if not posts:
                raise FeedUnavailable(f"Reddit unreachable: {errors}")
            return {"posts": posts, "errors": errors}

        return self._cached("reddit", load, ttl=3600)

    def podcasts(self, per_show: int = 3) -> dict[str, Any]:
        def feed_url(name: str) -> str:
            def find():
                q = urllib.parse.quote(name)
                data = self._json(f"https://itunes.apple.com/search?media=podcast&limit=1&term={q}")
                results = data.get("results") or []
                if not results or not results[0].get("feedUrl"):
                    raise FeedUnavailable(f"podcast {name!r} not found")
                return results[0]["feedUrl"]

            return self._cached(f"podcast-url-{name}", find, ttl=7 * 86400)

        def load():
            episodes, errors = [], {}
            for name in PODCASTS:
                try:
                    root = ET.fromstring(self._fetch(feed_url(name)))  # noqa: S314 - see _rss
                    for item in list(root.iter("item"))[:per_show]:
                        title = re.sub(r"\s+", " ", item.findtext("title") or "").strip()
                        notes = re.sub(r"<[^>]+>", " ", item.findtext("description") or "")
                        notes = re.sub(r"\s+", " ", notes).strip()[:400]
                        if title:
                            episodes.append(
                                {
                                    "show": name,
                                    "title": title[:200],
                                    "notes": notes,
                                    "published": item.findtext("pubDate"),
                                }
                            )
                except (FeedUnavailable, ET.ParseError) as e:
                    errors[name] = str(e)[:200]
            if not episodes:
                raise FeedUnavailable(f"no podcast feed reachable: {errors}")
            return {"episodes": episodes, "errors": errors}

        return self._cached("podcasts", load, ttl=6 * 3600)

    # ---------------- economic data (FRED) ----------------
    def economic(self) -> dict[str, Any]:
        key = (local_config.load("fred") or {}).get("api_key")
        if not key:
            raise FeedUnavailable("no FRED API key configured (python -m ai_team.keys setup)")

        def series(series_id: str, limit: int) -> list[float]:
            url = (
                "https://api.stlouisfed.org/fred/series/observations?file_type=json&sort_order=desc"
                f"&series_id={series_id}&limit={limit}&api_key={urllib.parse.quote(key)}"
            )
            try:
                data = self._json(url)
            except FeedUnavailable as e:  # the URL carries the key: never let it reach a report
                raise FeedUnavailable(
                    str(e).replace(urllib.parse.quote(key), "<key>").replace(key, "<key>")
                ) from None
            values = [
                float(o["value"]) for o in data.get("observations", []) if o.get("value") not in (None, ".")
            ]
            if not values:
                raise FeedUnavailable(f"FRED returned no data for {series_id}")
            return values  # newest first

        def load():
            fed = series("FEDFUNDS", 6)
            cpi = series("CPIAUCSL", 18)
            y10 = series("DGS10", 40)
            usd = series("DTWEXBGS", 40)
            if len(cpi) < 16:
                raise FeedUnavailable("FRED CPI history too short")
            return {
                "fed_funds_pct": fed[0],
                "fed_funds_3m_change": fed[0] - fed[min(3, len(fed) - 1)],
                "cpi_yoy_pct": (cpi[0] / cpi[12] - 1) * 100,
                "cpi_yoy_3m_ago_pct": (cpi[3] / cpi[15] - 1) * 100,
                "us10y_pct": y10[0],
                "us10y_1m_change": y10[0] - y10[min(21, len(y10) - 1)],
                "dollar_1m_change_pct": (usd[0] / usd[min(21, len(usd) - 1)] - 1) * 100,
                "source": "FRED",
            }

        return self._cached("fred", load, ttl=6 * 3600)


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

    def headline_scores(self, headlines: list[str], kind: str = "news") -> dict[str, dict[str, Any]]:
        return {}  # offline runs always use the lexicon

    def fear_greed(self) -> dict[str, Any]:
        return {"value": 30, "label": "Fear", "week_ago": 45}

    def derivatives(self) -> dict[str, dict[str, Any]]:
        return {
            a: {"funding_annualised": 0.08, "open_interest": 1e6, "mark_price": 1.0} for a in FUTURES_SYMBOLS
        }

    def reddit(self) -> dict[str, Any]:
        titles = ["Bitcoin rally continues as ETF inflows hit record", "Exchange hack: funds stolen"]
        return {
            "posts": [{"title": t, "score": 100, "subreddit": "CryptoCurrency"} for t in titles],
            "errors": {},
        }

    def podcasts(self, per_show: int = 3) -> dict[str, Any]:
        return {
            "episodes": [
                {
                    "show": "synthetic",
                    "title": "Why Bitcoin adoption keeps growing",
                    "notes": "",
                    "published": None,
                }
            ],
            "errors": {},
        }

    def economic(self) -> dict[str, Any]:
        return {
            "fed_funds_pct": 4.0,
            "fed_funds_3m_change": -0.25,
            "cpi_yoy_pct": 2.8,
            "cpi_yoy_3m_ago_pct": 3.1,
            "us10y_pct": 4.1,
            "us10y_1m_change": -0.1,
            "dollar_1m_change_pct": -0.5,
            "source": "synthetic",
        }

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
