"""Telegram notifications (ai_team.notifier) with a fake Telegram API and gateway."""

import io
import json
import urllib.error
import urllib.parse
from datetime import UTC, datetime, timedelta

import pytest

from ai_team import notifier as nt


TOKEN = "123456:SECRET-token"  # noqa: S105 - fake token for tests
NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


class FakeTelegramHTTP:
    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail

    def open(self, req, timeout=None):
        method = req.full_url.rsplit("/", 1)[-1]
        params = dict(p.split("=", 1) for p in req.data.decode().split("&")) if req.data else {}
        self.calls.append((method, params))
        if self.fail:
            raise self.fail
        result = {
            "getMe": {"username": "my_trading_bot"},
            "getUpdates": [{"message": {"chat": {"id": 42}}}],
            "sendMessage": {"message_id": 1},
        }[method]
        return io.BytesIO(json.dumps({"ok": True, "result": result}).encode())


class FakeGateway:
    def __init__(self):
        self.decision_list = []
        self.pf = {"positions": {}, "equity": 10_100.0, "history": [], "realised": [], "unrealised": {}}

    def decisions(self):
        return list(self.decision_list)

    def portfolio(self):
        return self.pf

    def limits(self):
        return {"starting_equity": 10_000.0}


def buy(ts="2026-11-20T10:00:00+00:00", pid="p1", pair="ETH/USDT"):
    return {
        "ts": ts,
        "proposal_id": pid,
        "pair": pair,
        "side": "long",
        "approved": True,
        "fill": {"side": "buy", "price": 2000.0, "amount": 0.5, "pair": pair},
    }


def sell(pnl, reason="take profit hit (2100 >= 2090)", ts="2026-11-20T11:00:00+00:00"):
    return {
        "ts": ts,
        "proposal_id": "exit-p1",
        "pair": "ETH/USDT",
        "side": "exit",
        "approved": True,
        "reasons": [reason],
        "fill": {"side": "sell", "price": 2100.0, "amount": 0.5, "pnl": pnl},
    }


class TestMessages:
    def test_buy_message_uses_position_levels(self):
        text = nt.describe_decision(
            buy(), {"ETH/USDT": {"stop_loss": 1900.0, "take_profit": 2200.0, "strategy": "mean_reversion"}}
        )
        assert "CUMPĂRAT" in text and "ETH/USDT" in text and "1,000.00 USDT" in text
        assert "1,900.00" in text and "2,200.00" in text and "mean_reversion" in text

    @pytest.mark.parametrize(
        ("pnl", "reason", "icon", "why"),
        [
            (25.0, "take profit hit (x)", "✅", "take-profit"),
            (-30.0, "stop loss hit (x)", "🔻", "stop-loss"),
            (0.0, "exit approved", "🔻", "ieșire"),
        ],
    )
    def test_sell_message(self, pnl, reason, icon, why):
        text = nt.describe_decision(sell(pnl, reason), {})
        assert text.startswith(icon) and why in text and f"{pnl:+,.2f}" in text

    @pytest.mark.parametrize(
        "d",
        [
            {"approved": False, "reasons": ["confidence 0.4 < 0.55"]},
            {"approved": True},
            {"approved": True, "fill": {"side": "other"}},
        ],
    )
    def test_no_message_for_rejections_or_unknown(self, d):
        assert nt.describe_decision(d, {}) is None

    def test_weekly_report(self):
        pf = {
            "equity": 10_250.0,
            "history": [
                {"ts": (NOW - timedelta(days=9)).isoformat(), "equity": 9_900.0},
                {"ts": (NOW - timedelta(days=7, hours=1)).isoformat(), "equity": 10_000.0},
                {"ts": (NOW - timedelta(days=1)).isoformat(), "equity": 10_200.0},
            ],
            "realised": [
                {"ts": (NOW - timedelta(days=2)).isoformat(), "pnl": 300.0, "strategy": "mean_reversion"},
                {"ts": (NOW - timedelta(days=3)).isoformat(), "pnl": -50.0, "strategy": "swing"},
                {"ts": (NOW - timedelta(days=10)).isoformat(), "pnl": 999.0, "strategy": "old"},
            ],
            "unrealised": {"BTC/USDT": {"pnl": 12.5}},
        }
        scores = {
            "a": {"signals": 10, "hit_rate": 0.7, "weight": 1.1},
            "b": {"signals": 2, "hit_rate": 1.0, "weight": 1.08},  # too few to be listed
        }
        text = nt.weekly_report(pf, scores, NOW, 10_000.0)
        assert "+250.00 USDT (+2.50%)" in text  # against the equity a week ago
        assert "De la început: +2.50%" in text
        assert "Tranzacții închise: 2 (1 pe plus) · Profit: +250.00 USDT" in text
        assert "old" not in text
        assert text.index("mean_reversion") < text.index("swing")
        assert "BTC/USDT: +12.50 USDT" in text
        assert "a: 70% corecte din 10" in text and "b:" not in text

    def test_weekly_report_empty_portfolio(self):
        text = nt.weekly_report({}, {}, NOW)
        assert "niciuna" in text


class TestTelegram:
    def test_errors_never_contain_the_token(self):
        http = FakeTelegramHTTP(fail=urllib.error.URLError(f"cannot reach /bot{TOKEN}/sendMessage"))
        with pytest.raises(nt.TelegramError) as e:
            nt.Telegram(TOKEN, "42", opener=http).send("hi")
        assert TOKEN not in str(e.value) and "<token>" in str(e.value)

    def test_refusal_is_reported(self):
        class Refuse(FakeTelegramHTTP):
            def open(self, req, timeout=None):
                return io.BytesIO(json.dumps({"ok": False, "description": "Unauthorized"}).encode())

        with pytest.raises(nt.TelegramError, match="Unauthorized"):
            nt.Telegram(TOKEN, opener=Refuse()).call("getMe")

    def test_config_round_trip_and_invalid(self, tmp_path):
        p = tmp_path / "telegram.json"
        assert nt.load_config(p) is None
        nt.save_config(TOKEN, "42", p)
        assert nt.load_config(p) == {"token": TOKEN, "chat_id": "42"}
        p.write_text('{"token": ""}')
        assert nt.load_config(p) is None
        p.write_text("{broken")
        assert nt.load_config(p) is None


class TestNotifier:
    def make(self, tmp_path):
        http, gw = FakeTelegramHTTP(), FakeGateway()
        n = nt.Notifier(gw, tmp_path, nt.Telegram(TOKEN, "42", opener=http))
        return n, gw, http

    def texts(self, http):
        return [urllib.parse.unquote_plus(p["text"]) for m, p in http.calls if m == "sendMessage"]

    def test_first_run_announces_and_skips_history(self, tmp_path):
        n, gw, http = self.make(tmp_path)
        gw.decision_list = [buy()]  # happened before setup: not replayed
        assert n.tick(NOW) == 1
        assert "pornite" in self.texts(http)[0]
        assert n.tick(NOW) == 0

    def test_new_trades_are_sent_once(self, tmp_path):
        n, gw, http = self.make(tmp_path)
        n.tick(NOW)
        gw.decision_list = [buy(), {"ts": "x", "proposal_id": "r", "approved": False}, sell(25.0)]
        assert n.tick(NOW) == 2
        assert n.tick(NOW) == 0
        t = self.texts(http)
        assert "CUMPĂRAT" in t[1] and "VÂNDUT" in t[2]

    def test_failed_send_is_retried_without_duplicates(self, tmp_path):
        n, gw, http = self.make(tmp_path)
        n.tick(NOW)
        gw.decision_list = [buy(pid="a"), buy(pid="b", ts="2026-11-20T10:30:00+00:00")]
        calls = {"n": 0}
        real = n.telegram.send

        def flaky(text):
            calls["n"] += 1
            if calls["n"] == 2:
                raise nt.TelegramError("network down")
            real(text)

        n.telegram.send = flaky
        with pytest.raises(nt.TelegramError):
            n.tick(NOW)
        assert n.tick(NOW) == 1  # only the trade that failed is sent again
        assert sum("CUMPĂRAT" in t for t in self.texts(http)) == 2

    def test_weekly_report_when_due(self, tmp_path):
        n, _gw, http = self.make(tmp_path)
        n.tick(NOW)
        assert n.tick(NOW + timedelta(days=6)) == 0
        assert n.tick(NOW + timedelta(days=7)) == 1
        assert "RAPORT SĂPTĂMÂNAL" in self.texts(http)[-1]
        assert n.tick(NOW + timedelta(days=8)) == 0

    def test_unconfigured_notifier_does_nothing(self, tmp_path):
        assert nt.Notifier(FakeGateway(), tmp_path).tick(NOW) == 0


def test_setup_finds_chat_and_saves_only_locally(tmp_path, monkeypatch):
    monkeypatch.setattr(nt, "CONFIG_DIR", tmp_path / "cfg")
    http = FakeTelegramHTTP()
    out = []
    answers = iter([TOKEN, ""])
    rc = nt.setup(
        input_fn=lambda _: next(answers),
        out=out.append,
        telegram_factory=lambda token: nt.Telegram(token, opener=http),
    )
    assert rc == 0
    assert nt.load_config() == {"token": TOKEN, "chat_id": "42"}
    assert [m for m, _ in http.calls] == ["getMe", "getUpdates", "sendMessage"]
    assert all(TOKEN not in line for line in out)


def test_setup_rejects_malformed_token(tmp_path, monkeypatch):
    monkeypatch.setattr(nt, "CONFIG_DIR", tmp_path / "cfg")
    assert nt.setup(input_fn=lambda _: "nope", out=lambda _: None) == 1
    assert nt.load_config() is None


def test_supervisor_runs_the_notifier():
    import argparse
    from pathlib import Path

    from ai_team import supervisor

    a = argparse.Namespace(
        gateway_port=1,
        dashboard_port=2,
        dashboard_host="127.0.0.1",
        data="synthetic",
        interval=5,
        timeframe="4h",
        gateway_state_dir=Path("gw"),
    )
    child = next(c for c in supervisor.build_children(a, Path("st")) if c.name == "notifier")
    assert child.args[:3] == ["-m", "ai_team.notifier", "run"]
