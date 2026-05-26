"""
scanner.py — MakStanleyz signal engine
1m RSI-turn + vol spike entry, 5m trend filter gate.
"""
import asyncio
import logging
import numpy as np
import pandas as pd
import ccxt.async_support as ccxt
from dataclasses import dataclass
from typing import Optional
import config

logger = logging.getLogger(__name__)


@dataclass
class Signal:
    symbol:     str
    direction:  str
    score:      int
    price:      float
    rsi:        float
    vol_ratio:  float
    change_24h: float
    is_gem:     bool
    is_gainer:  bool
    is_loser:   bool
    fund_rate:  float = 0.0


def calc_rsi(closes: np.ndarray, period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    deltas   = np.diff(closes[-(period + 1):])
    gains    = np.where(deltas > 0, deltas, 0.0)
    losses   = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = gains.mean()
    avg_loss = losses.mean()
    if avg_loss == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def calc_ema(values: np.ndarray, period: int) -> np.ndarray:
    ema = np.zeros_like(values, dtype=float)
    ema[0] = values[0]
    k = 2.0 / (period + 1)
    for i in range(1, len(values)):
        ema[i] = values[i] * k + ema[i - 1] * (1 - k)
    return ema


def calc_vol_ratio(volumes: np.ndarray, window: int = 20) -> float:
    if len(volumes) < window + 1:
        return 1.0
    avg = volumes[-(window + 1):-1].mean()
    if avg == 0:
        return 1.0
    return float(volumes[-1] / avg)


def calc_score(rsi: float, vol_ratio: float, change_24h: float,
               direction: str, fund_rate: float = 0.0) -> int:
    score = 0
    if direction == "long":
        score += 40 if rsi < 20 else 25 if rsi < 25 else 10 if rsi < 30 else 0
        score += 30 if vol_ratio >= 5 else 15 if vol_ratio >= 3 else 0
        score += 20 if change_24h < -15 else 10 if change_24h < -8 else 5 if change_24h < -3 else 0
        score += 10 if fund_rate <= config.FUND_RATE_STRONG else 5 if fund_rate <= config.FUND_RATE_MILD else 0
    else:
        score += 40 if rsi > 80 else 25 if rsi > 75 else 10 if rsi > 70 else 0
        score += 30 if vol_ratio >= 5 else 15 if vol_ratio >= 3 else 0
        score += 20 if change_24h > 15 else 10 if change_24h > 8 else 5 if change_24h > 3 else 0
        score += 10 if fund_rate >= -config.FUND_RATE_STRONG else 5 if fund_rate >= -config.FUND_RATE_MILD else 0
    return min(score, 100)


class MakStanleyzScanner:
    def __init__(self):
        self.exchange = ccxt.binanceusdm({"sandbox": False})

    async def fetch_ohlcv(self, symbol: str, tf: str = None,
                          limit: int = None) -> Optional[pd.DataFrame]:
        tf    = tf    or config.TIMEFRAME
        limit = limit or config.CANDLES_NEEDED
        try:
            raw = await self.exchange.fetch_ohlcv(symbol, tf, limit=limit)
            if not raw or len(raw) < 30:
                return None
            df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
            df["ts"] = pd.to_datetime(df["ts"], unit="ms")
            return df
        except Exception as e:
            logger.debug("fetch_ohlcv %s %s: %s", symbol, tf, e)
            return None

    async def fetch_ticker(self, symbol: str) -> Optional[dict]:
        try:
            return await self.exchange.fetch_ticker(symbol)
        except Exception as e:
            logger.debug("fetch_ticker %s: %s", symbol, e)
            return None

    async def fetch_funding_rate(self, symbol: str) -> float:
        try:
            data = await self.exchange.fetch_funding_rate(symbol)
            return float(data.get("fundingRate", 0.0) or 0.0)
        except Exception as e:
            logger.debug("fetch_funding_rate %s: %s", symbol, e)
            return 0.0

    def _htf5_state(self, df5m: pd.DataFrame) -> int:
        """
        5m trend state from EMA50 vs last close.
        Returns 1=bull, -1=bear, 0=neutral (±2.0% band).
        """
        if df5m is None or len(df5m) < 51:
            return 0
        closes = df5m["close"].values
        ema50  = calc_ema(closes, 50)[-1]
        price  = closes[-1]
        band   = 0.020
        if price > ema50 * (1 + band):
            return 1
        if price < ema50 * (1 - band):
            return -1
        return 0

    async def fetch_top_movers(self) -> list[tuple[str, str]]:
        try:
            tickers = await self.exchange.fetch_tickers()
        except Exception as e:
            logger.debug("fetch_top_movers unavailable: %s", e)
            return []

        movers = []
        for sym, t in tickers.items():
            if ":USDT" not in sym:
                continue
            pct = t.get("percentage")
            vol = t.get("quoteVolume") or 0.0
            if pct is None or vol < config.TOP_MOVER_MIN_VOL:
                continue
            movers.append((sym, float(pct)))

        if not movers:
            return []

        movers.sort(key=lambda x: x[1])
        n       = config.TOP_MOVERS_COUNT
        losers  = [(sym, "long")  for sym, _ in movers[:n]]
        gainers = [(sym, "short") for sym, _ in movers[-n:]]
        return losers + gainers

    async def scan_symbol(self, symbol: str,
                          forced_direction: str = "") -> Optional[Signal]:
        df_1m, df_5m, ticker, fund_rate = await asyncio.gather(
            self.fetch_ohlcv(symbol, config.TIMEFRAME),
            self.fetch_ohlcv(symbol, config.HTF_TIMEFRAME, limit=60),
            self.fetch_ticker(symbol),
            self.fetch_funding_rate(symbol),
        )
        if df_1m is None or ticker is None:
            return None

        closes  = df_1m["close"].values
        highs   = df_1m["high"].values
        lows    = df_1m["low"].values
        volumes = df_1m["volume"].values

        rsi_now  = calc_rsi(closes)
        rsi_prev = calc_rsi(closes[:-1])
        vol_curr = calc_vol_ratio(volumes)
        vol_prev = calc_vol_ratio(volumes[:-1])
        change_24h = ticker.get("percentage", 0.0) or 0.0
        price      = ticker.get("last", closes[-1])

        is_gainer = change_24h >= config.GAINER_THRESHOLD
        is_loser  = change_24h <= config.LOSER_THRESHOLD

        # 5m trend filter state
        htf5 = self._htf5_state(df_5m)

        def _long_confirmed() -> bool:
            return (rsi_prev < config.RSI_OVERSOLD
                    and vol_prev >= config.VOL_SPIKE_MULT
                    and rsi_now >= rsi_prev + 2.0
                    and closes[-1] > (highs[-1] + lows[-1]) / 2
                    and vol_curr < vol_prev
                    and htf5 >= 0)   # 5m not bearish

        def _short_confirmed() -> bool:
            return (rsi_prev > config.RSI_OVERBOUGHT
                    and vol_prev >= config.VOL_SPIKE_MULT
                    and rsi_now <= rsi_prev - 2.0
                    and closes[-1] < (highs[-1] + lows[-1]) / 2
                    and vol_curr < vol_prev
                    and htf5 <= 0)   # 5m not bullish

        if forced_direction:
            direction = forced_direction
            if direction == "long"  and not _long_confirmed():
                return None
            if direction == "short" and not _short_confirmed():
                return None
        else:
            direction = None
            if _long_confirmed():
                direction = "long"
            if _short_confirmed():
                direction = "short"
            if direction is None:
                return None

        score = calc_score(rsi_prev, vol_prev, change_24h, direction, fund_rate)
        if score < config.MIN_SCORE:
            return None

        return Signal(
            symbol=symbol,
            direction=direction,
            score=score,
            price=float(price),
            rsi=round(rsi_now, 1),
            vol_ratio=round(vol_curr, 1),
            change_24h=round(change_24h, 1),
            is_gem=(score >= 80),
            is_gainer=is_gainer,
            is_loser=is_loser,
            fund_rate=round(fund_rate, 6),
        )

    async def scan_all(self) -> list[Signal]:
        movers = await self.fetch_top_movers()
        if movers:
            scan_pairs = movers
        else:
            logger.debug("Top movers unavailable — scan cycle skipped")
            return []

        tasks   = [self.scan_symbol(sym, forced_dir) for sym, forced_dir in scan_pairs]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        signals = [r for r in results if isinstance(r, Signal)]
        signals.sort(key=lambda s: s.score, reverse=True)
        return signals

    async def close(self):
        await self.exchange.close()


# Alias for compatibility with webhook_app imports
SnitcherScanner = MakStanleyzScanner
