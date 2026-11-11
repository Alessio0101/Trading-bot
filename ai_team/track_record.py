"""
Learning from the team's own signals.

Every long signal an agent emits is written once per candle to a ledger. After a fixed
horizon the ledger scores it against what the market then did, net of the same fees and
slippage the backtests use. Each agent's record becomes a bounded weight that the
signal-aggregation specialist applies to its votes: agents that have been right more
often count up to 25 % more, agents that have been wrong count up to 25 % less.

The weight only changes how votes are combined. It never touches the independent
strategy reviews or the risk gateway's limits, which every proposal must still pass.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pandas as pd

from .data import TIMEFRAME_MINUTES, MarketData
from .fsutil import atomic_write_text
from .models import Report, utcnow


HORIZON_BARS = 6  # a 4-hour signal is judged one day later
ROUND_TRIP_COST = 2 * (0.0026 + 0.0005)  # fee + slippage per side, as in the backtests
PRIOR = 10  # pseudo-signals at a 50 % hit rate: a short record barely moves the weight
WEIGHT_MIN, WEIGHT_MAX = 0.75, 1.25
MAX_RECORDS = 5000


def weight_from(wins: int, n: int) -> float:
    """Shrunk hit rate mapped to a vote multiplier in [WEIGHT_MIN, WEIGHT_MAX]."""
    hit = (wins + PRIOR / 2) / (n + PRIOR)
    return round(min(max(0.5 + hit, WEIGHT_MIN), WEIGHT_MAX), 3)


class SignalLedger:
    _lock = threading.Lock()

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def _load(self) -> list[dict[str, Any]]:
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return []

    def _save(self, records: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(self.path, json.dumps(records[-MAX_RECORDS:]))

    def record(self, reports: dict[str, Report], pair: str, timeframe: str, data: MarketData) -> int:
        """Store each agent's long signal for ``pair`` once per candle; returns how many were new."""
        longs = {a: s for a, r in reports.items() for s in r.signals if s.pair == pair and s.side == "long"}
        if not longs:
            return 0
        candles = data.ohlcv(pair, timeframe, 5)
        candle = candles.index[-1].isoformat()
        price = float(candles["close"].iloc[-1])
        with self._lock:
            records = self._load()
            seen = {(r["agent"], r["pair"], r["timeframe"], r["candle"]) for r in records}
            new = 0
            for agent, s in longs.items():
                if (agent, pair, timeframe, candle) in seen:
                    continue
                records.append(
                    {
                        "agent": agent,
                        "pair": pair,
                        "timeframe": timeframe,
                        "strategy": s.strategy,
                        "candle": candle,
                        "ts": utcnow(),
                        "price": price,
                        "outcome": None,
                    }
                )
                new += 1
            if new:
                self._save(records)
        return new

    def evaluate(self, data: MarketData, now: pd.Timestamp | None = None) -> int:
        """Score every signal whose horizon has fully passed; returns how many were scored."""
        now = now or pd.Timestamp.now(tz="UTC")
        with self._lock:
            records = self._load()
            scored = 0
            cache: dict[tuple[str, str], pd.DataFrame] = {}
            for r in records:
                if r["outcome"] is not None:
                    continue
                step = pd.Timedelta(minutes=TIMEFRAME_MINUTES[r["timeframe"]])
                target = pd.Timestamp(r["candle"]) + step * HORIZON_BARS
                if now < target + step:  # the candle that holds the target must have closed
                    continue
                key = (r["pair"], r["timeframe"])
                if key not in cache:
                    try:
                        cache[key] = data.ohlcv(r["pair"], r["timeframe"], 3000)
                    except Exception:  # data outage: try again next cycle
                        cache[key] = pd.DataFrame()
                df = cache[key]
                past = df[df.index <= target] if len(df) else df
                if not len(past) or past.index[-1] < target - step:
                    continue  # no candle near the target (gap or data outage)
                exit_price = float(past["close"].iloc[-1])
                net = exit_price / r["price"] - 1 - ROUND_TRIP_COST
                r["outcome"] = {"exit_price": exit_price, "net_return": round(net, 6), "win": net > 0}
                scored += 1
            if scored:
                self._save(records)
        return scored

    def scores(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for r in self._load():
            if r["outcome"] is None:
                continue
            s = out.setdefault(r["agent"], {"signals": 0, "wins": 0, "net_return_sum": 0.0})
            s["signals"] += 1
            s["wins"] += int(r["outcome"]["win"])
            s["net_return_sum"] += r["outcome"]["net_return"]
        for s in out.values():
            s["hit_rate"] = round(s["wins"] / s["signals"], 3)
            s["avg_net_return"] = round(s.pop("net_return_sum") / s["signals"], 6)
            s["weight"] = weight_from(s["wins"], s["signals"])
        return out

    def pending(self) -> int:
        return sum(1 for r in self._load() if r["outcome"] is None)

    def weights(self) -> dict[str, float]:
        return {a: s["weight"] for a, s in self.scores().items()}
