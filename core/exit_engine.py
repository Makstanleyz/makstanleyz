"""
exit_engine.py — Triple-confirm exit engine (1m candles)
Evaluates RSI, EMA stack, VWAP on every 1m candle close.
"""
import logging
import numpy as np
import pandas as pd
import ccxt.async_support as ccxt
from core.order_executor import ActivePosition
from core.scanner import calc_rsi
import config

logger = logging.getLogger(__name__)


class ExitEngine:
    def __init__(self, exchange: ccxt.Exchange):
        self.exchange = exchange

    async def _fetch_candles(self, symbol: str, limit: int = 50) -> pd.DataFrame:
        raw = await self.exchange.fetch_ohlcv(symbol, config.TIMEFRAME, limit=limit)
        df  = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
        df["ts"] = pd.to_datetime(df["ts"], unit="ms")
        return df

    def _calc_ema(self, values: np.ndarray, period: int) -> np.ndarray:
        ema    = np.zeros_like(values)
        ema[0] = values[0]
        k = 2 / (period + 1)
        for i in range(1, len(values)):
            ema[i] = values[i] * k + ema[i - 1] * (1 - k)
        return ema

    def _calc_vwap(self, df: pd.DataFrame) -> float:
        hlc3   = (df["high"] + df["low"] + df["close"]) / 3
        cumvol = df["volume"].cumsum()
        vwap   = (hlc3 * df["volume"]).cumsum() / cumvol
        return float(vwap.iloc[-1])

    async def evaluate(self, position: ActivePosition) -> dict:
        try:
            df = await self._fetch_candles(position.symbol)
        except Exception as e:
            logger.error("Exit engine fetch error: %s", e)
            return {"error": str(e), "should_exit": False}

        closes     = df["close"].values
        rsi        = calc_rsi(closes)
        ema5       = self._calc_ema(closes, config.EMA_FAST)[-1]
        ema8       = self._calc_ema(closes, config.EMA_MID)[-1]
        ema13      = self._calc_ema(closes, config.EMA_SLOW)[-1]
        ema_bull   = ema5 > ema8 > ema13
        ema_bear   = ema5 < ema8 < ema13
        vwap       = self._calc_vwap(df)
        last_close = float(closes[-1])
        dir        = position.direction

        if dir == "long":
            rsi_holds  = rsi < config.RSI_HOLD_LONG
            ema_holds  = ema_bull
            vwap_holds = last_close > vwap
        else:
            rsi_holds  = rsi > config.RSI_HOLD_SHORT
            ema_holds  = ema_bear
            vwap_holds = last_close < vwap

        flips      = sum([not rsi_holds, not ema_holds, not vwap_holds])
        all_flipped = flips == 3

        if all_flipped:
            position.confirm_count += 1
        else:
            position.confirm_count = 0
        confirmed_exit = position.confirm_count >= config.EXIT_CONFIRM_BARS

        trail_hit = (
            dir == "long"  and last_close <= position.trail_stop or
            dir == "short" and last_close >= position.trail_stop
        )
        should_exit = confirmed_exit or trail_hit

        return {
            "rsi":            round(rsi, 1),
            "ema_bull":       ema_bull,
            "ema_bear":       ema_bear,
            "vwap":           round(vwap, 4),
            "last_close":     round(last_close, 4),
            "rsi_holds":      rsi_holds,
            "ema_holds":      ema_holds,
            "vwap_holds":     vwap_holds,
            "flips":          flips,
            "confirm_count":  position.confirm_count,
            "confirmed_exit": confirmed_exit,
            "trail_hit":      trail_hit,
            "should_exit":    should_exit,
            "exit_reason": (
                "TRAIL_STOP"     if trail_hit
                else "TRIPLE_CONFIRM" if confirmed_exit
                else f"CONFIRMING_{position.confirm_count}" if all_flipped
                else f"CAUTION_{flips}_of_3" if flips > 0
                else "HOLD"
            ),
        }
