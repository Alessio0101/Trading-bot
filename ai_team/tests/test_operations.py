"""Tests for the long-running system: runner, live market client, protective exits, dashboard API."""

import io
import json
import threading
import urllib.error
import urllib.request

import pytest

from ai_team.data import KrakenPublicData, KrakenUnavailable, SyntheticMarketData
from ai_team.gateway_client import GatewayClient
from ai_team.models import Report, Signal
from ai_team.state import SIGNAL_AGENTS, signal_view


class FakeOpener:
    """Returns queued responses (dict bodies or exceptions) for Kraken calls."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.urls = []

    def open(self, req, timeout=None):
        self.urls.append(req.full_url)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return io.BytesIO(json.dumps(r).encode())


TICKER = {
    "c": ["75000.1", "1"],
    "b": ["75000.0", "1", "1"],
    "a": ["75000.2", "1", "1"],
    "o": "74000.0",
    "h": ["75500", "76000"],
    "l": ["73900", "73000"],
    "v": ["10", "250"],
    "p": ["74800", "74500"],
    "t": [100, 5000],
}


@pytest.fixture(autouse=True)
def no_throttle(monkeypatch):
    monkeypatch.setattr(KrakenPublicData, "MIN_INTERVAL", 0)
    monkeypatch.setattr("ai_team.data.time.sleep", lambda s: None)


class TestKrakenPublicData:
    def test_ticker_maps_kraken_symbols(self):
        opener = FakeOpener(
            [
                {"error": [], "result": {"XBTUSDT": {"wsname": "XBT/USDT"}}},
                {"error": [], "result": {"XBTUSDT": TICKER}},
            ]
        )
        t = KrakenPublicData(opener=opener).ticker(["BTC/USDT"])
        assert list(t) == ["BTC/USDT"]
        assert t["BTC/USDT"]["last"] == pytest.approx(75000.1)
        assert t["BTC/USDT"]["change_pct_utc_day"] == pytest.approx((75000.1 / 74000 - 1) * 100)

    def test_transient_errors_are_retried(self):
        opener = FakeOpener(
            [
                urllib.error.URLError("timeout"),
                {"error": ["EService:Unavailable"]},
                {"error": [], "result": {"XBTUSDT": {"wsname": "XBT/USDT"}}},
            ]
        )
        assert KrakenPublicData(opener=opener)._pair_names(["BTC/USDT"]) == {"XBTUSDT": "BTC/USDT"}
        assert len(opener.urls) == 3

    def test_persistent_outage_raises(self):
        opener = FakeOpener([{"error": ["EService:Unavailable"]}] * 3)
        with pytest.raises(KrakenUnavailable, match="after 3 attempts"):
            KrakenPublicData(opener=opener)._get("Ticker", pair="BTCUSDT")

    def test_permanent_error_is_not_retried(self):
        opener = FakeOpener([{"error": ["EQuery:Unknown asset pair"]}])
        with pytest.raises(KrakenUnavailable, match="Unknown asset pair"):
            KrakenPublicData(opener=opener)._get("Ticker", pair="NOPE")
        assert len(opener.urls) == 1


class MutablePrices:
    kind = "test"

    def __init__(self, prices):
        self.prices = dict(prices)
        self.fail = False

    def price(self, pair):
        if self.fail:
            raise KrakenUnavailable("down")
        return self.prices[pair]

    def price_or_last(self, pair):
        if self.fail:
            return self.prices[pair], True
        return self.prices[pair], False


def _proposal(**kw):
    p = {
        "proposal_id": "p1",
        "pair": "BTC/USDT",
        "side": "long",
        "confidence": 0.7,
        "stake": 500.0,
        "entry_price": 100.0,
        "stop_loss": 97.0,
        "take_profit": 105.0,
        "strategy": "trend_following",
        "supporting_agents": ["trend-following-strategist"],
        "reviews": [
            {"reviewer": "backtesting-specialist", "verdict": "APPROVE"},
            {"reviewer": "independent-risk-reviewer", "verdict": "APPROVE"},
        ],
        "submitted_by": "trading-coordinator",
    }
    return {**p, **kw}


class TestProtectiveExits:
    @pytest.fixture
    def gw(self, gateway):
        gateway.prices = MutablePrices({"BTC/USDT": 100.0})
        assert gateway.evaluate(_proposal())["approved"]
        return gateway

    @pytest.mark.parametrize(("price", "reason"), [(96.0, "stop loss hit"), (106.0, "take profit hit")])
    def test_exit_executes(self, gw, price, reason):
        gw.prices.prices["BTC/USDT"] = price
        decisions = gw.check_exits()
        assert len(decisions) == 1 and reason in decisions[0]["reasons"][0]
        assert decisions[0]["origin"] == "gateway-protective-exit"
        snap = gw.snapshot()
        assert snap["positions"] == {} and len(snap["realised"]) == 1
        assert gw.health()["protective_exits_executed"] == 1

    def test_no_exit_inside_band(self, gw):
        gw.prices.prices["BTC/USDT"] = 101.0
        assert gw.check_exits() == []
        assert "BTC/USDT" in gw.snapshot()["positions"]
        assert gw.health()["last_price_check"]["ok"] is True

    def test_price_outage_is_reported_not_fatal(self, gw):
        gw.prices.fail = True
        assert gw.check_exits() == []
        assert gw.health()["last_price_check"]["ok"] is False

    def test_stale_prices_block_new_approvals(self, gw):
        gw.prices.fail = True
        d = gw.evaluate(_proposal(proposal_id="p2", pair="ETH/USDT"))
        assert not d["approved"] and "stale" in d["reasons"][0]

    def test_missing_price_blocks_approval(self, gateway):
        gateway.prices = MutablePrices({})
        gateway.prices.price = lambda pair: (_ for _ in ()).throw(KrakenUnavailable("down"))
        d = gateway.evaluate(_proposal())
        assert not d["approved"] and "price feed unavailable" in d["reasons"][0]


class TestSignalView:
    @pytest.mark.parametrize(
        ("signals", "action"),
        [
            ([], "HOLD"),
            ([Signal("BTC/USDT", "long", 0.6, "x", "r", strategy="s")], "BUY"),
            ([Signal("BTC/USDT", "exit", 0.6, "x", "r", strategy="s")], "SELL"),
        ],
    )
    def test_strategy_actions(self, signals, action):
        r = Report("trend-following-strategist", "t", "ok", "sum", {"strategy": "s"}, signals)
        assert signal_view(r)["action"] == action

    def test_not_run_is_na(self):
        assert signal_view(Report("options-strategist", "t", "needs_integration", "x"))["action"] == "N/A"

    def test_aggregator_uses_top_proposal(self):
        r = Report(
            "signal-aggregation-specialist",
            "t",
            "ok",
            "x",
            {"proposals": [{"side": "long", "confidence": 0.7, "strategy": "breakout", "votes": 2}]},
        )
        v = signal_view(r)
        assert v["action"] == "BUY" and v["confidence"] == 0.7 and v["strategy"] == "breakout"

    def test_aggregator_without_proposals_holds(self):
        r = Report("signal-aggregation-specialist", "t", "ok", "x", {"proposals": []})
        assert signal_view(r)["action"] == "HOLD"


def test_runner_cycle_invokes_all_agents(tmp_path, gateway_url):
    from ai_team.runner import Runner

    r = Runner(
        "synthetic", gateway_url, tmp_path, interval=1, market_interval=1, pairs=["BTC/USDT", "ETH/USDT"]
    )
    market = r.poll_market()
    assert market["ok"] and set(market["pairs"]) == {"BTC/USDT", "ETH/USDT"}
    result = r.run_cycle()
    assert result["ok"], result
    assert result["agents_invoked"] == 50 == result["agents_expected"]
    status = json.loads((tmp_path / "runner.json").read_text())
    assert status["cycles_completed"] == 1 and status["mode"] == "paper" and status["live_trading"] is False
    board = json.loads((tmp_path / "signals.json").read_text())
    assert set(board) == {"BTC/USDT", "ETH/USDT"}
    assert all(set(SIGNAL_AGENTS) == set(v) for v in board.values())


def test_runner_cycle_failure_is_reported(tmp_path, monkeypatch):
    from ai_team import runner as runner_mod

    r = runner_mod.Runner(
        "synthetic", "http://127.0.0.1:9", tmp_path, pairs=["BTC/USDT"], use_langgraph=False
    )
    monkeypatch.setattr(
        runner_mod.Coordinator, "cycle", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    result = r.run_cycle()
    assert result["ok"] is False and "boom" in result["error"]
    assert r.status["cycles_failed"] == 1 and r.status["state"] == "idle"


class TestDashboardApi:
    @pytest.fixture
    def server(self, tmp_path, gateway_url):
        from http.server import ThreadingHTTPServer

        from ai_team.dashboard.server import HTML_FILE, make_handler
        from ai_team.runner import Runner

        r = Runner("synthetic", gateway_url, tmp_path, pairs=["BTC/USDT"])
        r.poll_market()
        r.run_cycle()
        srv = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            make_handler(tmp_path, GatewayClient(gateway_url), HTML_FILE.read_bytes(), stream_interval=0.1),
        )
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.gateway_url = gateway_url
        yield f"http://127.0.0.1:{srv.server_address[1]}"
        srv.shutdown()

    def test_smoke_script_passes_against_real_page(self, server, monkeypatch, capsys):
        from ai_team.dashboard import smoke

        argv = ["smoke", "--dashboard", server, "--gateway", self.gateway_url, "--timeout", "10"]
        monkeypatch.setattr("sys.argv", argv)
        assert smoke.main() == 0
        assert "dashboard OK: 50 agents, gateway paper" in capsys.readouterr().out

    @staticmethod
    def get(url, timeout=10):
        return urllib.request.build_opener(urllib.request.ProxyHandler({})).open(url, timeout=timeout)

    def test_state_reflects_running_system(self, server):
        s = json.load(self.get(server + "/api/state"))
        assert len(s["agents"]) == 50
        assert all(a["performance"]["runs"] > 0 for a in s["agents"])
        assert s["market"]["pairs"]["BTC/USDT"]["last"] > 0
        assert "BTC/USDT" in s["signals"]["board"]
        assert s["gateway"]["health"]["mode"] == "paper"
        assert s["runner"]["cycles_completed"] == 1
        assert any(w["active"] for w in s["collaboration"]["workflows"])
        assert s["performance"]["strategies_by_pair"]["BTC/USDT"]["verdicts"]
        # The cycle just saved a heartbeat, so the runner counts as alive and raises no runner alert.
        assert s["runner"]["alive"] is True
        assert not [a for a in s["alerts"] if "Runner" in a["text"]]

    def test_stream_pushes_state(self, server):
        with self.get(server + "/api/stream") as resp:
            assert resp.headers["Content-Type"] == "text/event-stream"
            events = 0
            while events < 2:
                line = resp.readline().decode()
                if line.startswith("data: "):
                    assert len(json.loads(line[6:])["agents"]) == 50
                    events += 1
        assert events == 2

    def test_agent_detail(self, server):
        d = json.load(self.get(server + "/api/agent/volume-analyst"))
        assert d["state"]["id"] == "volume-analyst" and d["report"]["status"] == "ok"
        with pytest.raises(urllib.error.HTTPError) as e:
            self.get(server + "/api/agent/not-an-agent")
        assert e.value.code == 404

    def test_alerts_for_stale_market_and_stopped_runner(self, tmp_path):
        from ai_team.dashboard.server import collect

        (tmp_path / "runner.json").write_text(
            json.dumps({"state": "stopped", "heartbeat": "2026-01-01T00:00:00+00:00"})
        )
        (tmp_path / "market.json").write_text(
            json.dumps(
                {
                    "ts": "2026-01-01T00:00:00+00:00",
                    "ok": False,
                    "error": "KrakenUnavailable: down",
                    "pairs": {},
                }
            )
        )
        s = collect(tmp_path, GatewayClient("http://127.0.0.1:9", timeout=1))
        texts = " | ".join(a["text"] for a in s["alerts"])
        assert "Runner is stopped" in texts and "Kraken feed error" in texts and "stale" in texts
        assert s["runner"]["alive"] is False


def test_synthetic_ticker_matches_ohlcv():
    d = SyntheticMarketData()
    assert d.ticker(["BTC/USDT"])["BTC/USDT"]["last"] == pytest.approx(
        d.ohlcv("BTC/USDT", "1h", 5)["close"].iloc[-1]
    )
