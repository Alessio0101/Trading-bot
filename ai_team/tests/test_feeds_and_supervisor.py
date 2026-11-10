"""Public data feeds, the agents that use them, log trimming and the supervisor."""

import io
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ai_team.data import SyntheticMarketData
from ai_team.feeds import MAX_BYTES, FeedUnavailable, PublicFeeds
from ai_team.models import Task
from ai_team.runtime import AgentContext, run_specialist


RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
<item><title>Bitcoin ETF inflows hit record</title><link>https://x/1</link>
<pubDate>Tue, 10 Nov 2026 10:00:00 +0000</pubDate></item>
<item><title>Exchange hack: hackers stole funds</title><link>https://x/2</link>
<pubDate>Tue, 10 Nov 2026 11:00:00 +0000</pubDate></item>
<item><title>Solana launches new upgrade</title><link>https://x/3</link></item>
</channel></rss>"""


class FakeOpener:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def open(self, req, timeout=None):
        url = req.full_url
        self.calls.append(url)
        for prefix, body in self.routes.items():
            if url.startswith(prefix):
                if isinstance(body, Exception):
                    raise body
                return io.BytesIO(body if isinstance(body, bytes) else json.dumps(body).encode())
        raise urllib.error.URLError(f"no route for {url}")


@pytest.fixture(autouse=True)
def fresh_cache(monkeypatch):
    monkeypatch.setattr(PublicFeeds, "_cache", {})
    monkeypatch.setattr("ai_team.feeds.time.sleep", lambda s: None)


def feeds(routes):
    return PublicFeeds(opener=FakeOpener(routes))


class TestPublicFeeds:
    def test_news_filters_by_asset_and_falls_back(self):
        f = feeds(
            {
                "https://www.coindesk.com": RSS,
                "https://cointelegraph.com": RSS,
                "https://decrypt.co": urllib.error.URLError("down"),
            }
        )
        btc = f.news("BTC")
        assert btc["asset_specific"] and all("Bitcoin" in i["headline"] for i in btc["items"])
        assert btc["errors"].keys() == {"Decrypt"} and btc["sources"] == ["CoinDesk", "Cointelegraph"]
        ada = f.news("ADA")
        assert not ada["asset_specific"] and len(ada["items"]) == 6

    def test_news_all_feeds_down_raises(self):
        f = feeds({})
        with pytest.raises(FeedUnavailable, match="no news feed reachable"):
            f.news("BTC")

    def test_oversized_response_is_refused(self):
        f = feeds({"https://api.coingecko.com/api/v3/global": b"x" * (MAX_BYTES + 1)})
        with pytest.raises(FeedUnavailable):
            f.global_market()

    def test_bad_xml_is_reported(self):
        f = feeds(
            {
                "https://www.coindesk.com": b"<rss><unclosed>",
                "https://cointelegraph.com": RSS,
                "https://decrypt.co": RSS,
            }
        )
        assert "could not be parsed" in f.news("BTC")["errors"]["CoinDesk"]

    def test_fundamentals_global_second_venue_options(self):
        f = feeds(
            {
                "https://api.coingecko.com/api/v3/coins/markets": [{"id": "bitcoin", "market_cap": 1.0}],
                "https://api.coingecko.com/api/v3/global": {
                    "data": {"market_cap_change_percentage_24h_usd": -2}
                },
                "https://api.exchange.coinbase.com/products/BTC-USDT": {
                    "price": "10",
                    "bid": "9",
                    "ask": "11",
                },
                "https://api.exchange.coinbase.com/products/ADA-USDT": {"message": "NotFound"},
                "https://www.deribit.com": {"result": [{"instrument_name": "BTC-1JAN27-1-C"}]},
            }
        )
        assert f.fundamentals(["BTC"]) == {"BTC": {"id": "bitcoin", "market_cap": 1.0}}
        assert f.global_market()["market_cap_change_percentage_24h_usd"] == -2
        assert f.second_venue(["BTC/USDT", "ADA/USDT"]) == {
            "BTC/USDT": {"last": 10.0, "bid": 9.0, "ask": 11.0, "time": None}
        }
        assert f.options("BTC")[0]["instrument_name"] == "BTC-1JAN27-1-C"
        with pytest.raises(FeedUnavailable, match="only"):
            f.options("ADA")

    def test_responses_are_cached(self):
        opener = FakeOpener({"https://api.coingecko.com/api/v3/global": {"data": {"x": 1}}})
        f = PublicFeeds(opener=opener)
        f.global_market()
        f.global_market()
        assert len(opener.calls) == 1


def run(agent_id, data, pair="BTC/USDT", inbox=None, params=None):
    return run_specialist(
        agent_id, AgentContext(Task(agent_id, "t", pair, "1h", params or {}), data, inbox or {})
    )


class TestRepairedAgents:
    @pytest.fixture
    def market(self):
        return SyntheticMarketData(seed=11, drift=0.0012)

    @pytest.mark.parametrize(
        "agent_id",
        [
            "news-analyst",
            "macro-economic-analyst",
            "fundamental-analyst",
            "machine-learning-researcher",
            "arbitrage-strategist",
            "options-strategist",
        ],
    )
    def test_agent_runs_on_feeds(self, market, agent_id):
        r = run(agent_id, market)
        assert r.status == "ok", r.summary
        assert r.metrics["data_source"] == "synthetic"

    def test_news_to_event_chain(self, market):
        news = run("news-analyst", market)
        sent = run("sentiment-analyst", market, inbox={"news-analyst": news})
        event = run("event-driven-specialist", market, inbox={"sentiment-analyst": sent})
        assert sent.status == event.status == "ok"
        assert "adoption" in event.metrics["tags"]

    def test_task_headlines_override_feed(self, market):
        r = run("news-analyst", market, params={"headlines": ["Bitcoin hack shocks market"]})
        assert r.metrics["sources"] == ["task"] and r.metrics["headlines"][0]["tags"] == ["security"]

    def test_feed_outage_is_needs_integration(self, market, monkeypatch):
        def down(*a, **k):
            raise FeedUnavailable("down")

        for name in ("news", "global_market", "fundamentals", "second_venue", "options"):
            monkeypatch.setattr(market.feeds, name, down)
        for agent_id in (
            "news-analyst",
            "macro-economic-analyst",
            "fundamental-analyst",
            "arbitrage-strategist",
            "options-strategist",
        ):
            r = run(agent_id, market)
            assert r.status == "needs_integration" and "down" in r.summary, agent_id

    def test_options_only_btc_eth(self, market):
        r = run("options-strategist", market, pair="ADA/USDT")
        assert r.status == "needs_integration" and "only" in r.summary

    def test_arbitrage_detects_net_opportunity(self, market, monkeypatch):
        book = market.order_book("BTC/USDT")
        ask = book["asks"][0][0]
        monkeypatch.setattr(
            market.feeds,
            "second_venue",
            lambda pairs: {
                "BTC/USDT": {"last": ask * 1.02, "bid": ask * 1.02, "ask": ask * 1.021, "time": None}
            },
        )
        r = run("arbitrage-strategist", market)
        assert r.metrics["opportunity"] and r.metrics["net_bps"] > 0
        assert r.signals == []  # analysis only: never a cross-venue proposal

    def test_options_reads_nearest_expiry(self, market, monkeypatch):
        exp = (datetime.now(UTC) + timedelta(days=10)).strftime("%d%b%y").upper()
        rows = [
            {
                "instrument_name": f"BTC-{exp}-{k}-{cp}",
                "underlying_price": 100.0,
                "mark_iv": iv,
                "open_interest": oi,
            }
            for k, cp, iv, oi in [
                (90, "P", 70, 30),
                (100, "C", 80, 10),
                (100, "P", 82, 20),
                (110, "C", 75, 10),
            ]
        ]
        rows.append(
            {
                "instrument_name": "BTC-01JAN20-100-C",
                "underlying_price": 100,
                "mark_iv": 1,
                "open_interest": 999,
            }
        )  # expired: must be ignored for the ATM read
        monkeypatch.setattr(market.feeds, "options", lambda asset: rows)
        m = run("options-strategist", market).metrics
        assert m["atm_strike"] == 100 and m["atm_iv_pct"] == pytest.approx(81)
        assert m["put_call_oi_ratio"] == pytest.approx(50 / 1019)

    def test_ml_reports_edge_verdict(self, market):
        m = run("machine-learning-researcher", market).metrics
        assert m["folds"] >= 3 and 0 <= m["p_up"] <= 1
        assert m["verdict"] in ("edge", "no reliable edge")

    def test_rl_researcher_scores_policy_out_of_sample(self, market):
        r = run("reinforcement-learning-researcher", market)
        assert r.status == "ok", r.summary
        m = r.metrics
        assert m["algorithm"] == "tabular Q-learning" and m["research_only"] is True
        assert m["out_of_sample"]["bars"] == 720 - int(720 * 0.7)
        oos, hold = m["out_of_sample"], m["baseline_buy_hold"]
        assert m["beats_buy_hold"] == (oos["total_return"] > hold["total_return"])
        assert set(m["learned_policy_when_flat"].values()) <= {"long", "flat"}
        assert r.signals == []  # research only: never proposes trades
        again = run("reinforcement-learning-researcher", market).metrics
        assert again["out_of_sample"] == m["out_of_sample"]  # seeded, reproducible


def test_runner_and_supervisor_default_to_4h_bars():
    from ai_team import runner, supervisor
    from ai_team.runtime import DEFAULT_TIMEFRAME

    assert DEFAULT_TIMEFRAME == "4h"
    assert runner.Runner.__init__.__defaults__ is not None
    assert "4h" in runner.Runner.__init__.__defaults__
    import argparse

    a = argparse.Namespace(
        gateway_port=1,
        dashboard_port=2,
        dashboard_host="127.0.0.1",
        data="synthetic",
        interval=5,
        timeframe="4h",
    )
    runner_args = next(c.args for c in supervisor.build_children(a, Path()) if c.name == "runner")
    assert runner_args[runner_args.index("--timeframe") + 1] == "4h"


def test_trim_jsonl(tmp_path):
    from ai_team.runner import trim_jsonl

    p = tmp_path / "x.jsonl"
    p.write_text("".join(f'{{"i": {i}}}\n' for i in range(130)))
    assert trim_jsonl(p, 100) == 30
    lines = p.read_text().splitlines()
    assert len(lines) == 100 and json.loads(lines[0])["i"] == 30 and json.loads(lines[-1])["i"] == 129
    assert trim_jsonl(p, 100) == 0  # under the slack: untouched
    assert trim_jsonl(tmp_path / "missing.jsonl", 10) == 0


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait(pred, timeout):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            if pred():
                return True
        except (OSError, ValueError, KeyError):
            pass
        time.sleep(0.5)
    return False


def test_supervisor_restarts_children_and_stops_on_request(tmp_path):
    gw_port, dash_port = _free_port(), _free_port()
    root = Path(__file__).resolve().parents[2]
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "ai_team.supervisor",
            "--data",
            "synthetic",
            "--interval",
            "3600",
            "--gateway-port",
            str(gw_port),
            "--dashboard-port",
            str(dash_port),
            "--state-dir",
            str(tmp_path),
        ],
        cwd=root,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={**os.environ, "PYTHONPATH": str(root)},
    )
    status = tmp_path / "supervisor.json"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        assert _wait(lambda: opener.open(f"http://127.0.0.1:{dash_port}/api/state", timeout=2), 60)
        assert _wait(
            lambda: all(c["running"] for c in json.loads(status.read_text())["children"].values()), 30
        )
        runner_pid = json.loads(status.read_text())["children"]["runner"]["pid"]
        os.kill(runner_pid, 9)
        assert _wait(
            lambda: (
                json.loads(status.read_text())["children"]["runner"]["restarts"] == 1
                and json.loads(status.read_text())["children"]["runner"]["running"]
            ),
            40,
        )
        assert json.loads(status.read_text())["children"]["runner"]["pid"] != runner_pid
        (tmp_path / "supervisor.stop").write_text("stop")
        proc.wait(timeout=40)
        final = json.loads(status.read_text())
        assert "stopped_at" in final and not any(c["running"] for c in final["children"].values())
        assert not (tmp_path / "supervisor.stop").exists()
    finally:
        if proc.poll() is None:
            proc.kill()


def test_partly_supported_agent_stays_online(tmp_path):
    """Options exist for BTC but not ADA: the agent must not show offline because ADA ran last."""
    from ai_team.models import Report
    from ai_team.state import StateStore

    store = StateStore(tmp_path)
    ok = Report("options-strategist", "t1", "ok", "BTC ATM IV 40%", finished_at="2026-11-10T17:40:00+00:00")
    na = Report(
        "options-strategist",
        "t2",
        "needs_integration",
        "no options for ADA",
        finished_at="2026-11-10T17:40:05+00:00",
    )
    store.finish(ok, "BTC/USDT")
    store.finish(na, "ADA/USDT")
    a = store.agents["options-strategist"]
    assert a["online"] is True and a["status"] == "idle"
    assert a["by_pair"]["ADA/USDT"]["status"] == "needs_integration"

    only_na = StateStore(tmp_path / "other")
    only_na.finish(na, "ADA/USDT")
    assert only_na.agents["options-strategist"]["online"] is False
