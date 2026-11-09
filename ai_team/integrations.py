"""
Adapters for the external frameworks the team is designed to use.

Nothing is reimplemented here: each adapter only detects whether the framework
is importable and exposes the single entry point the team uses. Frameworks that
are not installed are reported as such - they are never simulated.
"""

from __future__ import annotations

import importlib
import importlib.util
from functools import cache
from typing import Any


FRAMEWORKS: dict[str, dict[str, Any]] = {
    "tradingagents": {
        "name": "TradingAgents",
        "module": "tradingagents",
        "used_by": [
            "technical-analyst",
            "news-analyst",
            "sentiment-analyst",
            "fundamental-analyst",
            "event-driven-specialist",
        ],
        "install": "pip install tradingagents (TauricResearch/TradingAgents)",
    },
    "finrobot": {
        "name": "FinRobot",
        "module": "finrobot",
        "used_by": [
            "news-analyst",
            "sentiment-analyst",
            "macro-economic-analyst",
            "fundamental-analyst",
            "event-driven-specialist",
        ],
        "install": "pip install finrobot (AI4Finance-Foundation/FinRobot)",
    },
    "rdagent": {
        "name": "RD-Agent",
        "module": "rdagent",
        "used_by": [
            "quantitative-researcher",
            "factor-researcher",
            "machine-learning-researcher",
            "parameter-optimisation-specialist",
            "reinforcement-learning-researcher",
        ],
        "install": "pip install rdagent (microsoft/RD-Agent)",
    },
    "qlib": {
        "name": "Qlib",
        "module": "qlib",
        "used_by": [
            "quantitative-researcher",
            "factor-researcher",
            "machine-learning-researcher",
            "backtesting-specialist",
            "parameter-optimisation-specialist",
            "reinforcement-learning-researcher",
        ],
        "install": "pip install pyqlib (microsoft/qlib)",
    },
    "langgraph": {
        "name": "LangGraph",
        "module": "langgraph",
        "used_by": ["trading-coordinator"],
        "install": "pip install langgraph",
    },
    "nautilus_trader": {
        "name": "NautilusTrader",
        "module": "nautilus_trader",
        "used_by": [
            "backtesting-specialist",
            "execution-trader",
            "order-routing-specialist",
            "scalping-strategist",
            "arbitrage-strategist",
            "options-strategist",
            "algorithmic-execution-specialist",
            "reinforcement-learning-researcher",
        ],
        "install": "pip install nautilus_trader",
    },
    "freqtrade": {
        "name": "freqtrade (paper bot)",
        "module": "freqtrade",
        "used_by": ["algorithmic-trading-engineer", "strategy-deployment-engineer"],
        "install": "in this repository / easy-start docker image",
    },
}


@cache
def status(key: str) -> dict:
    fw = FRAMEWORKS[key]
    installed = importlib.util.find_spec(fw["module"]) is not None
    version = None
    if installed:
        try:
            version = getattr(importlib.import_module(fw["module"]), "__version__", "unknown")
        except Exception as e:
            return {**fw, "key": key, "installed": False, "error": f"import failed: {e}"}
    return {**fw, "key": key, "installed": installed, "version": version}


def all_status() -> list[dict]:
    return [status(k) for k in FRAMEWORKS]
