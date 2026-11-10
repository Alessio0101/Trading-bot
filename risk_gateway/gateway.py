"""Risk evaluation and paper broker. No code path here can place a live order."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


HERE = Path(__file__).resolve().parent
LIMITS_FILE = HERE / "limits.json"

# The gateway reuses the team's read-only market data client; make the repo root
# importable when the gateway is started from another directory.
if str(HERE.parent) not in sys.path:
    sys.path.insert(0, str(HERE.parent))

from ai_team.data import KrakenPublicData, SyntheticMarketData  # noqa: E402


HISTORY_LIMIT = 5000  # equity marks kept for the dashboard curve

ALLOWED_FIELDS = {
    "proposal_id",
    "pair",
    "side",
    "confidence",
    "stake",
    "entry_price",
    "stop_loss",
    "take_profit",
    "strategy",
    "supporting_agents",
    "reviews",
    "rationale",
    "submitted_by",
}
REQUIRED_FIELDS = {"proposal_id", "pair", "side", "confidence", "strategy", "reviews", "submitted_by"}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class PriceSource:
    """The gateway prices orders itself; it never trusts the proposer's price."""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self._src: KrakenPublicData | SyntheticMarketData = (
            KrakenPublicData() if kind == "kraken" else SyntheticMarketData()
        )

        self._last: dict[str, float] = {}

    def price(self, pair: str) -> float:
        """Fresh price or an exception; used for every approval and fill."""
        px = float(self._src.ticker([pair])[pair]["last"])
        self._last[pair] = px
        return px

    def price_or_last(self, pair: str) -> tuple[float, bool]:
        """For valuation/display only: fall back to the last known price, flagged stale."""
        try:
            return self.price(pair), False
        except Exception:
            if pair in self._last:
                return self._last[pair], True
            raise


class RiskGateway:
    def __init__(self, state_dir: Path, price_source: PriceSource) -> None:
        self.limits: dict[str, Any] = json.loads(LIMITS_FILE.read_text(encoding="utf-8"))
        self.limits_sha256 = hashlib.sha256(LIMITS_FILE.read_bytes()).hexdigest()
        if self.limits.get("mode") != "paper":
            raise SystemExit("risk_gateway only supports mode='paper'; refusing to start")
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.portfolio_file = self.state_dir / "portfolio.json"
        self.log_file = self.state_dir / "decisions.jsonl"
        self.prices = price_source
        self._lock = threading.Lock()
        self.started_at = _now()
        self.exits_executed = 0
        self.last_price_check: dict[str, Any] = {"ts": None, "ok": None, "error": None}
        self.portfolio = self._load()

    # ---------------- persistence ----------------
    def _load(self) -> dict:
        if self.portfolio_file.exists():
            return json.loads(self.portfolio_file.read_text(encoding="utf-8"))
        eq = float(self.limits["starting_equity"])
        return {
            "cash": eq,
            "positions": {},
            "fills": [],
            "realised": [],
            "history": [{"ts": _now(), "equity": eq}],
            "peak_equity": eq,
            "day": _now()[:10],
            "day_start_equity": eq,
        }

    def _save(self) -> None:
        # Unique temporary name per process and thread, so concurrent writers never
        # replace (and delete) each other's file.
        tmp = self.portfolio_file.with_name(
            f"{self.portfolio_file.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        try:
            tmp.write_text(json.dumps(self.portfolio, indent=2), encoding="utf-8")
            # On Windows the replace fails while another process (e.g. the antivirus)
            # briefly holds the file open, so retry for up to ~5 s before giving up.
            for attempt in range(100):
                try:
                    tmp.replace(self.portfolio_file)
                    break
                except PermissionError:
                    if attempt == 99:
                        raise
                    time.sleep(0.05)
        finally:
            tmp.unlink(missing_ok=True)

    def _log(self, entry: dict) -> None:
        with self.log_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

    # ---------------- valuation ----------------
    def snapshot(self) -> dict:
        with self._lock:
            return self._snapshot()

    def _snapshot(self) -> dict:
        pf = self.portfolio
        unrealised, mv, stale = {}, 0.0, []
        for pair, p in pf["positions"].items():
            try:
                px, is_stale = self.prices.price_or_last(pair)
            except Exception:
                px, is_stale = p["entry_price"], True
            if is_stale:
                stale.append(pair)
            value = p["amount"] * px
            mv += value
            unrealised[pair] = {
                "price": px,
                "value": value,
                "strategy": p.get("strategy"),
                "pnl": value - p["amount"] * p["entry_price"],
            }
        equity = pf["cash"] + mv
        today = _now()[:10]
        if pf["day"] != today:
            pf["day"], pf["day_start_equity"] = today, equity
        pf["peak_equity"] = max(pf["peak_equity"], equity)
        return {
            **pf,
            "equity": equity,
            "exposure": mv,
            "unrealised": unrealised,
            "daily_pnl_pct": equity / pf["day_start_equity"] - 1,
            "drawdown_pct": equity / pf["peak_equity"] - 1,
            "stale_prices": stale,
        }

    # ---------------- decision ----------------
    def evaluate(self, proposal: dict) -> dict:
        with self._lock:
            decision = self._evaluate(proposal)
            self._log(decision)
            return decision

    def _evaluate(self, p: dict) -> dict:
        L = self.limits
        reasons: list[str] = []
        base = {
            "type": "gateway",
            "proposal_id": p.get("proposal_id"),
            "ts": _now(),
            "pair": p.get("pair"),
            "side": p.get("side"),
            "limits_sha256": self.limits_sha256,
        }

        structural = self._structural_error(p)
        if structural:
            return {**base, "approved": False, "reasons": [structural]}

        snap = self._snapshot()
        # Fail closed: never decide on stale or missing prices.
        if snap["stale_prices"]:
            return {
                **base,
                "approved": False,
                "reasons": [f"price feed stale for open positions {snap['stale_prices']}"],
            }
        try:
            price = self.prices.price(p["pair"])
        except Exception as e:
            return {
                **base,
                "approved": False,
                "reasons": [f"price feed unavailable: {type(e).__name__}: {e}"],
            }
        positions = snap["positions"]

        if p["side"] == "exit":
            if p["pair"] not in positions:
                return {**base, "approved": False, "reasons": ["no open position to exit"]}
            fill = self._close(p, price)
            return {**base, "approved": True, "reasons": ["exit approved"], "fill": fill}

        # Independent reviews: required reviewers must approve and must not be proposers.
        reviews = {r.get("reviewer"): r for r in p["reviews"] if isinstance(r, dict)}
        supporters = set(p.get("supporting_agents", []))
        for reviewer in L["required_reviewers"]:
            r = reviews.get(reviewer)
            if r is None or r.get("verdict") != "APPROVE":
                reasons.append(f"missing APPROVE from {reviewer}")
            if reviewer in supporters:
                reasons.append(f"{reviewer} cannot review a proposal it supports")
        if p["submitted_by"] != "trading-coordinator":
            reasons.append("proposals must be submitted by the trading-coordinator")
        if float(p["confidence"]) < L["min_confidence"]:
            reasons.append(f"confidence {p['confidence']} < {L['min_confidence']}")
        if snap["daily_pnl_pct"] <= -L["max_daily_loss_pct"]:
            reasons.append(f"daily loss limit hit ({snap['daily_pnl_pct']:.2%})")
        if snap["drawdown_pct"] <= -L["max_drawdown_pct"]:
            reasons.append(f"drawdown limit hit ({snap['drawdown_pct']:.2%})")
        if p["pair"] in positions:
            reasons.append("position already open for pair (no pyramiding)")
        if len(positions) >= L["max_open_positions"]:
            reasons.append(f"max open positions {L['max_open_positions']} reached")

        equity = snap["equity"]
        stake = float(p.get("stake") or 0)
        if stake < L["min_stake"]:
            reasons.append(f"stake {stake:.2f} < min {L['min_stake']}")
        if stake > equity * L["max_position_pct"]:
            reasons.append(f"stake {stake:.2f} > {L['max_position_pct']:.0%} of equity")
        if snap["exposure"] + stake > equity * L["max_total_exposure_pct"]:
            reasons.append("total exposure limit would be exceeded")
        if stake > snap["cash"]:
            reasons.append("insufficient paper cash")

        sl = p.get("stop_loss")
        if L["require_stop_loss"] and not sl:
            reasons.append("stop loss required")
        elif sl:
            if sl >= price:
                reasons.append("stop loss must be below the gateway price")
            elif (price - sl) / price > L["max_stop_distance_pct"]:
                reasons.append(f"stop distance {(price - sl) / price:.2%} > {L['max_stop_distance_pct']:.0%}")
        if p.get("entry_price") and abs(p["entry_price"] / price - 1) > L["max_price_deviation_pct"]:
            reasons.append("proposal price deviates from gateway price")

        if reasons:
            return {**base, "approved": False, "reasons": reasons, "gateway_price": price}
        fill = self._open(p, price, stake)
        return {
            **base,
            "approved": True,
            "reasons": ["all limits satisfied"],
            "fill": fill,
            "gateway_price": price,
        }

    def _structural_error(self, p: dict) -> str | None:
        unknown = set(p) - ALLOWED_FIELDS
        if unknown:
            return f"unknown fields rejected: {sorted(unknown)}"
        missing = REQUIRED_FIELDS - set(p)
        if missing:
            return f"missing fields: {sorted(missing)}"
        if p["pair"] not in self.limits["allowed_pairs"]:
            return f"pair {p['pair']} not allowed"
        if p["side"] not in ("long", "exit"):
            return f"side {p['side']!r} not allowed (spot paper)"
        return None

    # ---------------- paper broker ----------------
    def _fill_price(self, price: float, side: str) -> float:
        slip = self.limits["assumed_slippage_bps"] / 1e4
        return price * (1 + slip) if side == "buy" else price * (1 - slip)

    def _open(self, p: dict, price: float, stake: float) -> dict:
        fp = self._fill_price(price, "buy")
        fee = stake * self.limits["fee_rate"]
        amount = (stake - fee) / fp
        pf = self.portfolio
        pf["cash"] -= stake
        pf["positions"][p["pair"]] = {
            "amount": amount,
            "entry_price": fp,
            "stop_loss": p.get("stop_loss"),
            "take_profit": p.get("take_profit"),
            "strategy": p["strategy"],
            "supporting_agents": p.get("supporting_agents", []),
            "proposal_id": p["proposal_id"],
            "opened_at": _now(),
        }
        fill = {
            "proposal_id": p["proposal_id"],
            "pair": p["pair"],
            "side": "buy",
            "price": fp,
            "amount": amount,
            "fee": fee,
            "ts": _now(),
            "venue": "paper",
        }
        pf["fills"].append(fill)
        self._mark()
        return fill

    def _close(self, p: dict, price: float) -> dict:
        pf = self.portfolio
        pos = pf["positions"].pop(p["pair"])
        fp = self._fill_price(price, "sell")
        gross = pos["amount"] * fp
        fee = gross * self.limits["fee_rate"]
        pf["cash"] += gross - fee
        pnl = gross - fee - pos["amount"] * pos["entry_price"]
        pf["realised"].append(
            {
                "pair": p["pair"],
                "pnl": pnl,
                "strategy": pos["strategy"],
                "supporting_agents": pos["supporting_agents"],
                "ts": _now(),
            }
        )
        fill = {
            "proposal_id": p["proposal_id"],
            "pair": p["pair"],
            "side": "sell",
            "price": fp,
            "amount": pos["amount"],
            "fee": fee,
            "pnl": pnl,
            "ts": _now(),
            "venue": "paper",
        }
        pf["fills"].append(fill)
        self._mark()
        return fill

    def _mark(self) -> None:
        snap = self._snapshot()
        history = self.portfolio["history"]
        history.append({"ts": _now(), "equity": snap["equity"]})
        del history[:-HISTORY_LIMIT]
        self._save()

    # ---------------- protective exits ----------------
    def check_exits(self) -> list[dict]:
        """
        Execute paper stop-loss / take-profit exits at the gateway's own price.

        Runs inside the gateway so protective exits never depend on the agents
        or the coordinator being alive. Also records an equity mark.
        """
        decisions = []
        with self._lock:
            try:
                for pair, pos in list(self.portfolio["positions"].items()):
                    price = self.prices.price(pair)
                    reason = None
                    if pos.get("stop_loss") and price <= pos["stop_loss"]:
                        reason = f"stop loss hit ({price:.6g} <= {pos['stop_loss']:.6g})"
                    elif pos.get("take_profit") and price >= pos["take_profit"]:
                        reason = f"take profit hit ({price:.6g} >= {pos['take_profit']:.6g})"
                    if reason:
                        fill = self._close({"pair": pair, "proposal_id": f"exit-{pos['proposal_id']}"}, price)
                        d = {
                            "type": "gateway",
                            "proposal_id": fill["proposal_id"],
                            "ts": _now(),
                            "pair": pair,
                            "side": "exit",
                            "approved": True,
                            "reasons": [reason],
                            "origin": "gateway-protective-exit",
                            "fill": fill,
                            "limits_sha256": self.limits_sha256,
                        }
                        self._log(d)
                        decisions.append(d)
                        self.exits_executed += 1
                self._mark()
                self.last_price_check = {"ts": _now(), "ok": True, "error": None}
            except Exception as e:
                self.last_price_check = {"ts": _now(), "ok": False, "error": f"{type(e).__name__}: {e}"}
        return decisions

    def health(self) -> dict:
        return {
            "ok": True,
            "mode": "paper",
            "live_trading": False,
            "limits_sha256": self.limits_sha256,
            "price_source": self.prices.kind,
            "pid": os.getpid(),
            "started_at": self.started_at,
            "open_positions": len(self.portfolio["positions"]),
            "protective_exits_executed": self.exits_executed,
            "last_price_check": self.last_price_check,
        }
