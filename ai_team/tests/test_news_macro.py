"""Claude headline scoring, FRED economic data and the optional-keys setup, all offline."""

import io
import json
from types import SimpleNamespace

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
