"""Claude's weekly review (ai_team.llm_review) with a fake client."""

from types import SimpleNamespace

from ai_team import llm_review


class Fake:
    def __init__(self, stop_reason="end_turn", error=None):
        self.calls = []
        self.stop_reason = stop_reason
        self.error = error

    def create(self, **kw):
        self.calls.append(kw)
        if self.error:
            raise self.error
        return SimpleNamespace(
            stop_reason=self.stop_reason, content=[SimpleNamespace(type="text", text=" Bine. ")]
        )


def client(fake):
    return SimpleNamespace(messages=fake, beta=SimpleNamespace(messages=fake))


def test_review_request_and_text():
    fake = Fake()
    text = llm_review.write_review({"equity": 10_100}, {"model": "claude-opus-5-5"}, client(fake))
    assert text == "Bine."
    kw = fake.calls[0]
    assert (
        kw["fallbacks"] == "default"
        and "Romanian" in kw["system"]
        and "never promise profits" in kw["system"]
    )
    assert '"equity": 10100' in kw["messages"][0]["content"]


def test_cheaper_model_uses_plain_messages():
    fake = Fake()
    llm_review.write_review({}, {"model": "claude-haiku-5-5"}, client(fake))
    assert "fallbacks" not in fake.calls[0]


def test_no_key_refusal_or_error_gives_no_review():
    assert llm_review.write_review({}, {}) is None
    assert (
        llm_review.write_review({}, {"model": "claude-opus-5-5"}, client(Fake(stop_reason="refusal"))) is None
    )
    assert llm_review.write_review({}, {}, client(Fake(error=RuntimeError("down")))) is None


def test_week_summary_is_compact():
    pf = {
        "equity": 10_050.0,
        "max_drawdown_pct": -0.02,
        "realised": [{"pair": "ETH/USDT", "strategy": "mean_reversion", "pnl": 12.0, "ts": "t", "extra": 1}]
        * 40,
        "unrealised": {"BTC/USDT": {"pnl": 3.456}},
    }
    ready = {
        "criteria": [{"name": "Closed trades", "value": "40", "target": ">= 30", "ok": True, "key": "trades"}]
    }
    reviews = {"ETH/USDT": {"verdicts": {"mean_reversion": {"verdict": "APPROVE", "reasons": []}}}}
    s = llm_review.week_summary(pf, ready, {}, reviews, {"adopted": 1, "tested": 25, "params": {"x": 1}})
    assert len(s["closed_trades"]) == 30 and "extra" not in s["closed_trades"][0]
    assert s["open_positions"] == {"BTC/USDT": 3.46}
    assert s["strategy_verdicts"] == {"ETH/USDT": {"mean_reversion": "APPROVE"}}
    assert s["tuning"] == {"updated_at": None, "adopted": 1, "tested": 25}
    assert "key" not in s["readiness"][0]
