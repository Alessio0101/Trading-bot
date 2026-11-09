import json
import urllib.request

import pytest
from risk_gateway import gateway as gw_mod

from ai_team.gateway_client import GatewayClient


def good(market, **kw):
    price = float(market.ohlcv("BTC/USDT", "1m", 5)["close"].iloc[-1])
    p = {
        "proposal_id": "p1",
        "pair": "BTC/USDT",
        "side": "long",
        "confidence": 0.7,
        "stake": 500.0,
        "entry_price": price,
        "stop_loss": price * 0.97,
        "take_profit": price * 1.05,
        "strategy": "trend_following",
        "supporting_agents": ["trend-following-strategist"],
        "reviews": [
            {"reviewer": "backtesting-specialist", "verdict": "APPROVE"},
            {"reviewer": "independent-risk-reviewer", "verdict": "APPROVE"},
        ],
        "submitted_by": "trading-coordinator",
    }
    p.update(kw)
    return p


def test_evaluate_approves_valid_proposal(gateway, market):
    d = gateway.evaluate(good(market))
    assert d["approved"], d["reasons"]
    assert d["fill"]["venue"] == "paper"
    assert "BTC/USDT" in gateway.snapshot()["positions"]


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"override_limits": True}, "unknown fields"),
        ({"submitted_by": "technical-analyst"}, "submitted by the trading-coordinator"),
        ({"reviews": []}, "missing APPROVE from backtesting-specialist"),
        (
            {
                "reviews": [
                    {"reviewer": "backtesting-specialist", "verdict": "APPROVE"},
                    {"reviewer": "independent-risk-reviewer", "verdict": "REJECT"},
                ]
            },
            "missing APPROVE from independent-risk-reviewer",
        ),
        ({"supporting_agents": ["independent-risk-reviewer"]}, "cannot review a proposal it supports"),
        ({"confidence": 0.3}, "confidence"),
        ({"stake": 5000.0}, "of equity"),
        ({"stake": 1.0}, "min"),
        ({"stop_loss": None}, "stop loss required"),
        ({"pair": "DOGE/USDT"}, "not allowed"),
        ({"side": "short"}, "not allowed"),
        ({"entry_price": 1.0}, "deviates"),
    ],
)
def test_evaluate_rejects(gateway, market, change, reason):
    d = gateway.evaluate(good(market, **change))
    assert not d["approved"], "proposal should be rejected"
    assert any(reason in r for r in d["reasons"]), d["reasons"]
    assert gateway.snapshot()["positions"] == {}, "no paper position should be opened"


def test_evaluate_limits_exposure_and_positions(gateway, market):
    for i, pair in enumerate(["BTC/USDT", "ETH/USDT", "SOL/USDT"]):
        price = float(market.ohlcv(pair, "1m", 5)["close"].iloc[-1])
        d = gateway.evaluate(
            good(
                market, proposal_id=f"p{i}", pair=pair, entry_price=price, stop_loss=price * 0.97, stake=900.0
            )
        )
        assert d["approved"], d["reasons"]
    price = float(market.ohlcv("XRP/USDT", "1m", 5)["close"].iloc[-1])
    d = gateway.evaluate(
        good(market, proposal_id="p9", pair="XRP/USDT", entry_price=price, stop_loss=price * 0.97)
    )
    assert any("max open positions" in r for r in d["reasons"])
    d = gateway.evaluate(good(market, proposal_id="p10"))
    assert any("no pyramiding" in r for r in d["reasons"])


def test_evaluate_exit_closes_position(gateway, market):
    gateway.evaluate(good(market))
    d = gateway.evaluate(
        {
            "proposal_id": "x1",
            "pair": "BTC/USDT",
            "side": "exit",
            "confidence": 0.6,
            "strategy": "trend_following",
            "reviews": [],
            "submitted_by": "trading-coordinator",
        }
    )
    assert d["approved"]
    snap = gateway.snapshot()
    assert snap["positions"] == {} and len(snap["realised"]) == 1


def test_evaluate_halts_on_daily_loss(gateway, market):
    gateway.portfolio["day_start_equity"] = 20000.0  # equity 10000 => -50% today
    gateway.portfolio["day"] = gw_mod._now()[:10]
    d = gateway.evaluate(good(market))
    assert any("daily loss limit" in r for r in d["reasons"])


def test_gateway_refuses_non_paper_mode(tmp_path, monkeypatch):
    f = tmp_path / "limits.json"
    data = json.loads(gw_mod.LIMITS_FILE.read_text())
    f.write_text(json.dumps({**data, "mode": "live"}))
    monkeypatch.setattr(gw_mod, "LIMITS_FILE", f)
    with pytest.raises(SystemExit):
        gw_mod.RiskGateway(tmp_path / "s", gw_mod.PriceSource("synthetic"))


def test_server_routes_are_read_only(gateway_url):
    c = GatewayClient(gateway_url)
    h = c.health()
    assert h["mode"] == "paper" and h["live_trading"] is False
    for method in ("PUT", "PATCH", "DELETE"):
        req = urllib.request.Request(gateway_url + "/limits", data=b"{}", method=method)  # noqa: S310
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req)
        assert e.value.code == 405
    assert c.limits()["read_only"] is True


def test_client_fails_closed_when_gateway_down():
    d = GatewayClient("http://127.0.0.1:9", timeout=1).submit({"proposal_id": "z"})
    assert d["approved"] is False and "fail closed" in d["reasons"][0]
