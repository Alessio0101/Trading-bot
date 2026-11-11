"""
Weekly re-tuning of each strategy's exit levels, kept only when unseen data agrees.

For every pair and rule-based strategy the stop-loss and take-profit distances (in ATR) are
chosen on the first 70 % of the candle history. The choice is adopted only if, on the final
30 % that played no part in choosing, it beats the default levels by a margin and still makes
money. Otherwise the defaults stay. This is the only thing that changes by itself: rules,
review standards and gateway limits do not.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from itertools import product
from pathlib import Path
from typing import Any

from .data import MarketData
from .fsutil import atomic_write_text
from .specialists.quant import HISTORY_BARS, OOS_FRACTION, backtest_with_stops
from .specialists.strategies import STOP_ATR, STRATEGY_RULES, TAKE_PROFIT_ATR


GRID_STOP = (1.5, 2.0, 2.5, 3.0)
GRID_TARGET = (2.0, 3.0, 4.0, 5.0)
MIN_IN_SAMPLE_TRADES = 5
OOS_MARGIN = 0.25  # out-of-sample Sharpe must beat the defaults by at least this much
EVERY = timedelta(days=7)
TUNED = [s for s in STRATEGY_RULES if s != "scalping"]  # scalping trades 1-minute bars

_lock = threading.Lock()


def tune_one(df, rule, timeframe: str) -> dict[str, Any]:
    split = int(len(df) * (1 - OOS_FRACTION))
    pos = rule(df)
    ins, oos = (df.iloc[:split], pos.iloc[:split]), (df.iloc[split:], pos.iloc[split:])
    best, best_sharpe = (STOP_ATR, TAKE_PROFIT_ATR), float("-inf")
    for sl, tp in product(GRID_STOP, GRID_TARGET):
        m = backtest_with_stops(*ins, timeframe, sl, tp)
        if m["trades"] >= MIN_IN_SAMPLE_TRADES and m["sharpe"] > best_sharpe:
            best, best_sharpe = (sl, tp), m["sharpe"]
    default_oos = backtest_with_stops(*oos, timeframe, STOP_ATR, TAKE_PROFIT_ATR)
    tuned_oos = backtest_with_stops(*oos, timeframe, *best)
    adopted = (
        best != (STOP_ATR, TAKE_PROFIT_ATR)
        and tuned_oos["trades"] > 0
        and tuned_oos["total_return"] > 0
        and tuned_oos["sharpe"] >= default_oos["sharpe"] + OOS_MARGIN
    )
    return {
        "stop_atr": best[0] if adopted else STOP_ATR,
        "take_profit_atr": best[1] if adopted else TAKE_PROFIT_ATR,
        "adopted": adopted,
        "candidate": list(best),
        "in_sample_sharpe": None if best_sharpe == float("-inf") else round(best_sharpe, 3),
        "oos_sharpe_default": round(default_oos["sharpe"], 3),
        "oos_sharpe_candidate": round(tuned_oos["sharpe"], 3),
        "oos_return_candidate": round(tuned_oos["total_return"], 4),
        "bars": len(df),
    }


class Tuner:
    def __init__(self, state_dir: Path) -> None:
        self.path = Path(state_dir) / "tuned_params.json"

    def load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def due(self, timeframe: str, now: datetime | None = None) -> bool:
        data = self.load()
        try:
            updated = datetime.fromisoformat(data["updated_at"])
        except (KeyError, TypeError, ValueError):
            return True
        return data.get("timeframe") != timeframe or (now or datetime.now(UTC)) - updated >= EVERY

    def run(self, data: MarketData, pairs: list[str], timeframe: str, now: datetime | None = None) -> dict:
        params: dict[str, dict[str, Any]] = {}
        errors: dict[str, str] = {}
        for pair in pairs:
            try:
                df = data.ohlcv(pair, timeframe, HISTORY_BARS)
            except Exception as e:  # one pair's data outage must not stop the others
                errors[pair] = f"{type(e).__name__}: {e}"[:200]
                continue
            params[pair] = {name: tune_one(df, STRATEGY_RULES[name], timeframe) for name in TUNED}
        result = {
            "updated_at": (now or datetime.now(UTC)).isoformat(timespec="seconds"),
            "timeframe": timeframe,
            "params": params,
            "errors": errors,
            "adopted": sum(1 for p in params.values() for v in p.values() if v["adopted"]),
            "tested": sum(len(p) for p in params.values()),
        }
        with _lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(self.path, json.dumps(result))
        return result

    def exit_levels(self, pair: str, timeframe: str) -> dict[str, list[float]]:
        """{strategy: [stop_atr, take_profit_atr]} for this pair; defaults when nothing was adopted."""
        data = self.load()
        if data.get("timeframe") != timeframe:
            return {}
        return {
            name: [v["stop_atr"], v["take_profit_atr"]]
            for name, v in (data.get("params") or {}).get(pair, {}).items()
            if v.get("adopted")
        }
