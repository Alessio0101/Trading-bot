"""
StrategieUsoara - a simple, beginner-friendly trend-following strategy.

Entry: the fast EMA crosses above the slow EMA while the price is above the
long-term EMA (uptrend) and RSI is not overbought.
Exit: the fast EMA crosses back below the slow EMA, or RSI becomes overbought.
Risk: fixed stop-loss, trailing stop and a minimal ROI table.

Every number marked as a parameter can be tuned with hyperopt or edited here.
"""

import talib.abstract as ta
from pandas import DataFrame
from technical import qtpylib

from freqtrade.strategy import IntParameter, IStrategy


class StrategieUsoara(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "15m"
    can_short = False

    # Take profit: 6% at any time, 3% after 2h, 1% after 6h.
    minimal_roi = {"0": 0.06, "120": 0.03, "360": 0.01}

    # Never lose more than 5% on a single trade.
    stoploss = -0.05

    # Lock in profit once a trade is 2% up, trailing 1% behind the peak.
    trailing_stop = True
    trailing_stop_positive = 0.01
    trailing_stop_positive_offset = 0.02
    trailing_only_offset_is_reached = True

    process_only_new_candles = True
    use_exit_signal = True
    exit_profit_only = False

    # Enough candles for the 200 EMA to settle.
    startup_candle_count: int = 250

    ema_fast = IntParameter(5, 20, default=9, space="buy")
    ema_slow = IntParameter(20, 60, default=21, space="buy")
    rsi_buy_max = IntParameter(50, 75, default=65, space="buy")
    rsi_sell = IntParameter(65, 90, default=75, space="sell")

    plot_config = {
        "main_plot": {
            "ema_trend": {"color": "gray"},
        },
        "subplots": {
            "RSI": {"rsi": {"color": "orange"}},
        },
    }

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        for period in set(self.ema_fast.range) | set(self.ema_slow.range):
            dataframe[f"ema_{period}"] = ta.EMA(dataframe, timeperiod=period)
        dataframe["ema_trend"] = ta.EMA(dataframe, timeperiod=200)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        fast = dataframe[f"ema_{self.ema_fast.value}"]
        slow = dataframe[f"ema_{self.ema_slow.value}"]
        dataframe.loc[
            (
                qtpylib.crossed_above(fast, slow)
                & (dataframe["close"] > dataframe["ema_trend"])
                & (dataframe["rsi"] < self.rsi_buy_max.value)
                & (dataframe["volume"] > 0)
            ),
            ["enter_long", "enter_tag"],
        ] = (1, "ema_cross_up")
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        fast = dataframe[f"ema_{self.ema_fast.value}"]
        slow = dataframe[f"ema_{self.ema_slow.value}"]
        dataframe.loc[
            (qtpylib.crossed_below(fast, slow) & (dataframe["volume"] > 0)),
            ["exit_long", "exit_tag"],
        ] = (1, "ema_cross_down")
        dataframe.loc[
            ((dataframe["rsi"] > self.rsi_sell.value) & (dataframe["volume"] > 0)),
            ["exit_long", "exit_tag"],
        ] = (1, "rsi_overbought")
        return dataframe
