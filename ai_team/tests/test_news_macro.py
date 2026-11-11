"""Claude headline scoring, FRED economic data and the optional-keys setup, all offline."""

import io
import json
from types import SimpleNamespace

import pandas as pd
import pytest

from ai_team import keys, llm_news, local_config
from ai_team.data import SyntheticMarketData
from ai_team.feeds import FeedUnavailable, PublicFeeds
from ai_team.models import Report, Task
from ai_team.runtime import AgentContext, run_specialist


FAKE_KEY = "sk-ant-test-0000"


@pytest.fixture(autouse=True)
def config_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_TEAM_CONFIG_DIR", str(tmp_path / "cfg"))
    PublicFeeds._cache.clear()
    return tmp_path / "cfg"


class FakeMessages:
    def __init__(self, items=None, stop_reason="end_turn", error=None):
        self.items = items
        self.stop_reason = stop_reason
        self.error = error
        self.calls = []

    def create(self, **kw):
        self.calls.append(kw)
        if self.error:
            raise self.error
        n = len(kw["messages"][0]["content"].splitlines()) - 1
        items = (
            self.items
            if self.items is not None
            else [{"index": i, "sentiment": 0.5, "event": "adoption", "assets": ["BTC"]} for i in range(n)]
        )
        text = SimpleNamespace(type="text", text=json.dumps({"items": items}))
        return SimpleNamespace(stop_reason=self.stop_reason, content=[text])


def fake_client(**kw):
    m = FakeMessages(**kw)
    return SimpleNamespace(messages=m, beta=SimpleNamespace(messages=m)), m


class TestClaudeNewsScorer:
    def scorer(self, tmp_path, model=None, **kw):
        client, msgs = fake_client(**kw)
        settings = {"api_key": FAKE_KEY, **({"model": model} if model else {})}
        return llm_news.ClaudeNewsScorer(tmp_path / "scores.json", settings, client=client), msgs

    def test_scores_once_and_caches(self, tmp_path):
        s, msgs = self.scorer(tmp_path)
        out = s.scores(["Bitcoin ETF approved", "Exchange hacked"], now=1000)
        assert set(out) == {"Bitcoin ETF approved", "Exchange hacked"}
        assert out["Exchange hacked"]["sentiment"] == 0.5 and out["Exchange hacked"]["event"] == "adoption"
        assert len(msgs.calls) == 1
        # cached: no new request, even after the hour has passed
        assert s.scores(["Bitcoin ETF approved"], now=1000 + llm_news.REFRESH_S + 1) == {
            "Bitcoin ETF approved": out["Bitcoin ETF approved"]
        }
        assert len(msgs.calls) == 1

    def test_new_headlines_wait_for_the_hourly_slot(self, tmp_path):
        s, msgs = self.scorer(tmp_path)
        s.scores(["a"], now=1000)
        assert s.scores(["a", "b"], now=1000 + 60) == {"a": s.scores(["a"], now=1000 + 60)["a"]}
        assert len(msgs.calls) == 1
        assert "b" in s.scores(["a", "b"], now=1000 + llm_news.REFRESH_S)
        assert len(msgs.calls) == 2
        assert "0. b" in msgs.calls[1]["messages"][0]["content"]  # only the unseen headline is sent

    def test_request_shape_with_fallbacks_and_untrusted_data(self, tmp_path):
        s, msgs = self.scorer(tmp_path)
        s.scores(["x"], now=1)
        kw = msgs.calls[0]
        assert kw["model"] == "claude-opus-5-5"
        assert kw["fallbacks"] == "default" and kw["betas"] == ["server-side-fallback-2026-07-01"]
        assert kw["output_config"]["format"]["type"] == "json_schema"
        assert "untrusted" in kw["system"] and "thinking" not in kw and "temperature" not in kw

    def test_model_without_fallbacks_uses_plain_messages(self, tmp_path):
        s, msgs = self.scorer(tmp_path, model="claude-haiku-5-5")
        s.scores(["x"], now=1)
        assert "fallbacks" not in msgs.calls[0] and "betas" not in msgs.calls[0]

    def test_bad_items_are_clamped_or_ignored(self, tmp_path):
        items = [
            {"index": 0, "sentiment": 7, "event": "nonsense", "assets": ["BTC", "DOGE"]},
            {"index": 99, "sentiment": -1, "event": "macro", "assets": []},
        ]
        s, _ = self.scorer(tmp_path, items=items)
        out = s.scores(["only"], now=1)
        assert out["only"] == {"sentiment": 1.0, "event": "general", "assets": ["BTC"], "ts": 1}

    @pytest.mark.parametrize("kw", [{"stop_reason": "refusal"}, {"error": RuntimeError("down")}])
    def test_failures_back_off_and_score_nothing(self, tmp_path, kw):
        s, msgs = self.scorer(tmp_path, **kw)
        assert s.scores(["x"], now=1) == {}
        assert s.status()["last_error"]
        assert s.scores(["x"], now=2) == {}  # retries only after the back-off
        assert len(msgs.calls) == 1

    def test_disabled_without_key(self, tmp_path):
        s = llm_news.ClaudeNewsScorer(tmp_path / "s.json", None)
        assert not s.enabled and s.scores(["x"]) == {}


class FakeFeeds:
    def __init__(self, scores=None, fail=False):
        self._scores = scores or {}
        self.fail = fail

    def headline_scores(self, headlines):
        if self.fail:
            raise RuntimeError("boom")
        return {h: self._scores[h] for h in headlines if h in self._scores}


def run_sentiment(feeds):
    market = SyntheticMarketData()
    market.feeds = feeds
    news = Report(
        "news-analyst",
        "t",
        "ok",
        "s",
        metrics={
            "headlines": [{"headline": "Bitcoin rally continues"}, {"headline": "Exchange hack drains funds"}]
        },
    )
    ctx = AgentContext(Task("sentiment-analyst", "t", "BTC/USDT", "4h", {}), market, {"news-analyst": news})
    return run_specialist("sentiment-analyst", ctx)


class TestSentimentAnalyst:
    def test_lexicon_without_claude(self):
        m = run_sentiment(FakeFeeds()).metrics
        assert m["method"] == "lexicon" and m["claude_scored"] == 0
        assert [s["score"] for s in m["scored"]] == [1.0, -1.0]

    def test_claude_scores_win_and_mix(self):
        claude = {
            "Exchange hack drains funds": {"sentiment": -0.9, "event": "security", "assets": ["crypto"]}
        }
        m = run_sentiment(FakeFeeds(claude)).metrics
        assert m["method"] == "mixed" and m["claude_scored"] == 1
        assert m["scored"][1]["score"] == -0.9 and m["scored"][1]["method"] == "claude"
        assert m["sentiment"] == pytest.approx((1.0 - 0.9) / 2)

    def test_scorer_failure_falls_back(self):
        r = run_sentiment(FakeFeeds(fail=True))
        assert r.status == "ok" and r.metrics["method"] == "lexicon"


def fred_body(values):
    return {"observations": [{"value": v} for v in values]}


class FredOpener:
    def __init__(self, fail=False):
        self.urls = []
        self.fail = fail

    def open(self, req, timeout=None):
        url = req.full_url
        self.urls.append(url)
        if self.fail:
            raise OSError(f"cannot reach {url}")
        sid = url.split("series_id=")[1].split("&")[0]
        data = {
            "FEDFUNDS": ["4.00", "4.25", "4.50", "4.50"],  # newest first: easing
            "CPIAUCSL": [str(330 - i) for i in range(18)],
            "DGS10": ["4.0", "."] + ["4.4"] * 38,
            "DTWEXBGS": ["120"] + ["118"] * 39,
        }[sid]
        return io.BytesIO(json.dumps(fred_body(data)).encode())


class TestFred:
    def test_economic_series(self):
        local_config.save("fred", {"api_key": "abc123"})
        e = PublicFeeds(opener=FredOpener()).economic()
        assert e["fed_funds_pct"] == 4.0 and e["fed_funds_3m_change"] == pytest.approx(-0.5)
        assert e["cpi_yoy_pct"] == pytest.approx((330 / 318 - 1) * 100)
        assert e["us10y_1m_change"] == pytest.approx(-0.4)  # the "." gap is skipped
        assert e["dollar_1m_change_pct"] == pytest.approx((120 / 118 - 1) * 100)

    def test_no_key(self):
        with pytest.raises(FeedUnavailable, match="no FRED API key"):
            PublicFeeds(opener=FredOpener()).economic()

    def test_errors_never_contain_the_key(self):
        local_config.save("fred", {"api_key": "supersecretkey"})
        with pytest.raises(FeedUnavailable) as e:
            PublicFeeds(opener=FredOpener(fail=True)).economic()
        assert "supersecretkey" not in str(e.value) and "<key>" in str(e.value)


def run_macro(market):
    ctx = AgentContext(Task("macro-economic-analyst", "t", "BTC/USDT", "4h", {}), market, {})
    return run_specialist("macro-economic-analyst", ctx)


class TestMacroAnalyst:
    def test_with_economic_data(self):
        r = run_macro(SyntheticMarketData())
        m = r.metrics
        assert r.status == "ok" and m["scope"].startswith("crypto market + US economy")
        assert "Fed easing" in m["economic_notes"] and "inflation falling" in m["economic_notes"]
        assert m["economic_bias"] == pytest.approx(0.5) and -1 <= m["risk_appetite"] <= 1
        assert "Fed funds 4.00%" in r.summary

    def test_without_economic_data(self):
        market = SyntheticMarketData()

        def down():
            raise FeedUnavailable("no FRED API key configured")

        market.feeds.economic = down
        m = run_macro(market).metrics
        assert m["scope"].startswith("crypto market only") and "economic" not in m


class TestKeysSetup:
    def test_saves_checked_keys_and_never_prints_them(self):
        out, answers = [], iter([FAKE_KEY, "", "fredkey"])
        rc = keys.setup(
            input_fn=lambda _: next(answers),
            out=out.append,
            checks={"anthropic": lambda k: None, "fred": lambda k: None},
        )
        assert rc == 0
        assert local_config.load("anthropic") == {"api_key": FAKE_KEY, "model": "claude-opus-5-5"}
        assert local_config.load("fred") == {"api_key": "fredkey"}
        assert not any(FAKE_KEY in line or "fredkey" in line for line in out)
        st = keys.status()
        assert st == {
            "anthropic": True,
            "anthropic_model": "claude-opus-5-5",
            "fred": True,
            "telegram": False,
        }
        assert FAKE_KEY not in json.dumps(st)

    def test_rejected_and_skipped_keys_are_not_saved(self):
        answers = iter(["bad", ""])
        keys.setup(
            input_fn=lambda _: next(answers),
            out=lambda _: None,
            checks={"anthropic": lambda k: "cheia nu este valida", "fred": lambda k: None},
        )
        assert local_config.load("anthropic") is None and local_config.load("fred") is None


def test_dashboard_lists_optional_services_without_secrets(tmp_path):
    from ai_team.dashboard.server import _optional_services

    local_config.save("anthropic", {"api_key": FAKE_KEY, "model": "claude-opus-5-5"})
    services = _optional_services(tmp_path)
    assert [s["installed"] for s in services] == [True, False, False]
    assert FAKE_KEY not in json.dumps(services)


def test_keys_are_read_without_echo(monkeypatch):
    prompts = []

    def fake_getpass(prompt):
        prompts.append(prompt)
        return ""

    monkeypatch.setattr(keys.getpass, "getpass", fake_getpass)
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("a key prompt must not echo"))
    keys.setup(out=lambda _: None, checks={"anthropic": lambda k: None, "fred": lambda k: None})
    assert len(prompts) == 2 and "Anthropic" in prompts[0] and "FRED" in prompts[1]
    assert keys.mask("sk-ant-abcdefgh1234") == "...1234 (19 caractere)"
    assert keys.mask("short") == "(5 caractere)"


# ---------------- crowd sources: Fear & Greed, futures, Reddit, podcasts ----------------

PODCAST_RSS = b"""<?xml version="1.0"?><rss><channel><title>p</title>
<item><title>Bitcoin adoption keeps growing</title>
<description>&lt;p&gt;Big &lt;b&gt;inflows&lt;/b&gt;&lt;/p&gt;</description></item>
<item><title>Second episode</title></item></channel></rss>"""


class RouteOpener:
    def __init__(self, routes):
        self.routes = routes
        self.urls = []

    def open(self, req, timeout=None):
        self.urls.append(req.full_url)
        for prefix, body in self.routes.items():
            if req.full_url.startswith(prefix):
                if isinstance(body, Exception):
                    raise body
                return io.BytesIO(body if isinstance(body, bytes) else json.dumps(body).encode())
        raise OSError(f"no route for {req.full_url}")


class TestCrowdFeeds:
    def test_fear_greed(self):
        body = {"data": [{"value": "20", "value_classification": "Extreme Fear"}, {"value": "55"}]}
        fg = PublicFeeds(opener=RouteOpener({"https://api.alternative.me": body})).fear_greed()
        assert fg == {"value": 20, "label": "Extreme Fear", "week_ago": 55}

    def test_derivatives_funding_is_relative_and_annualised(self):
        body = {
            "tickers": [
                {"symbol": "PF_XBTUSD", "markPrice": 100000, "fundingRate": 1.0, "openInterest": 5},
                {"symbol": "PF_ETHUSD", "markPrice": 0},
            ]
        }
        d = PublicFeeds(opener=RouteOpener({"https://futures.kraken.com": body})).derivatives()
        assert set(d) == {"BTC"}
        assert d["BTC"]["funding_annualised"] == pytest.approx(1.0 / 100000 * 24 * 365)

    def test_reddit_skips_stickies_and_tolerates_one_subreddit_down(self):
        posts = {
            "data": {
                "children": [
                    {"data": {"title": "Rules thread", "stickied": True}},
                    {"data": {"title": "Bitcoin  rally   continues", "score": 900}},
                ]
            }
        }
        opener = RouteOpener(
            {
                "https://www.reddit.com/r/CryptoCurrency": posts,
                "https://www.reddit.com/r/Bitcoin": OSError("429"),
            }
        )
        r = PublicFeeds(opener=opener).reddit()
        assert [p["title"] for p in r["posts"]] == ["Bitcoin rally continues"]
        assert "Bitcoin" in r["errors"]

    def test_podcasts_are_found_via_search_and_notes_are_plain_text(self):
        search = {"results": [{"feedUrl": "https://pod.example/feed"}]}
        opener = RouteOpener({"https://itunes.apple.com": search, "https://pod.example/feed": PODCAST_RSS})
        p = PublicFeeds(opener=opener).podcasts(per_show=1)
        first = p["episodes"][0]
        assert first["title"] == "Bitcoin adoption keeps growing" and first["notes"] == "Big inflows"
        assert len(p["episodes"]) == 5  # one per show, the same test feed for every show

    def test_everything_down(self):
        feeds = PublicFeeds(opener=RouteOpener({}))
        for call in (feeds.fear_greed, feeds.derivatives, feeds.reddit, feeds.podcasts):
            with pytest.raises(FeedUnavailable):
                call()


class TestCompositeSentiment:
    def run(self, weights=None):
        market = SyntheticMarketData()
        news = Report(
            "news-analyst", "t", "ok", "s", metrics={"headlines": [{"headline": "Bitcoin rally continues"}]}
        )
        params = {"source_weights": weights or {}}
        ctx = AgentContext(
            Task("sentiment-analyst", "t", "BTC/USDT", "4h", params), market, {"news-analyst": news}
        )
        return run_specialist("sentiment-analyst", ctx)

    def test_components_views_and_contrarian_reading(self):
        m = self.run().metrics
        c = m["components"]
        assert set(c) == {"news", "reddit", "podcasts", "fear_greed", "funding"}
        assert c["fear_greed"] == pytest.approx((50 - 30) / 40)  # fear -> bullish
        assert c["funding"] == pytest.approx(-0.08 / 0.3)  # longs paying -> bearish
        assert m["source_views"]["fear_greed"] == 1 and m["source_views"]["funding"] == -1
        assert m["sentiment"] == pytest.approx(sum(c.values()) / 5)
        assert m["source_errors"] == {}

    def test_learned_weights_shift_the_composite(self):
        even = self.run().metrics["sentiment"]
        tilted = self.run({"source:funding": 1.25, "source:fear_greed": 0.75}).metrics
        assert tilted["source_weights"]["funding"] == 1.25 and tilted["sentiment"] < even


def test_ledger_scores_bearish_views_when_price_falls(tmp_path):
    from ai_team.track_record import ROUND_TRIP_COST, SignalLedger

    class Falling:
        name = "test"

        def __init__(self, n):
            idx = pd.date_range("2026-01-01", periods=30, freq="4h", tz="UTC")
            self.df = pd.DataFrame({"close": [100.0] * 5 + [90.0] * 25}, index=idx).iloc[:n]

        def ohlcv(self, pair, timeframe="4h", limit=500):
            return self.df.tail(limit)

    ledger = SignalLedger(tmp_path / "l.json")
    assert ledger.record_views({"funding": -1, "reddit": 1, "news": 0}, "BTC/USDT", "4h", Falling(5)) == 2
    ledger.evaluate(Falling(30), now=pd.Timestamp("2026-01-10", tz="UTC"))
    s = ledger.scores()
    assert s["source:funding"]["wins"] == 1 and s["source:reddit"]["wins"] == 0
    assert s["source:funding"]["avg_net_return"] == pytest.approx(0.10 - ROUND_TRIP_COST)


def test_aggregation_adds_a_small_crowd_bias():
    from ai_team.models import Signal

    def agg(sentiment):
        inbox = {
            "momentum-strategist": Report(
                "momentum-strategist",
                "t",
                "ok",
                "s",
                signals=[Signal("BTC/USDT", "long", 0.6, "momentum-strategist", "x", strategy="momentum")],
            ),
            "sentiment-analyst": Report(
                "sentiment-analyst", "t", "ok", "s", metrics={"sentiment": sentiment}
            ),
        }
        ctx = AgentContext(
            Task("signal-aggregation-specialist", "t", "BTC/USDT", "4h", {}), SyntheticMarketData(), inbox
        )
        return run_specialist("signal-aggregation-specialist", ctx).metrics["proposals"][0]["confidence"]

    assert agg(1.0) - agg(0.0) == pytest.approx(0.05) and agg(-1.0) < agg(0.0)


def test_a_failed_feed_is_not_retried_for_a_while(monkeypatch):
    opener = RouteOpener({})
    feeds = PublicFeeds(opener=opener)
    with pytest.raises(FeedUnavailable):
        feeds.fear_greed()
    calls = len(opener.urls)
    with pytest.raises(FeedUnavailable):
        feeds.fear_greed()  # served from the failure cache
    assert len(opener.urls) == calls
    monkeypatch.setattr(PublicFeeds, "FAILURE_TTL_S", 0)
    with pytest.raises(FeedUnavailable):
        feeds.fear_greed()
    assert len(opener.urls) > calls
