"""
market_scanner.py — Market Context Engine (ADX regime gate)
ADX < 25  → both directions open, full 10x leverage
ADX 25–35 → both directions open, leverage capped at 5x
ADX > 35  → skip all entries (extreme trend, mean-reversion fails)
"""
import asyncio
import logging
import numpy as np
import pandas as pd
import ccxt.async_support as ccxt
from dataclasses import dataclass, field
from typing import Optional
import config

logger = logging.getLogger(__name__)


@dataclass
class MarketContext:
    btc_trend:    str   = "neutral"
    btc_adx:      float = 0.0
    btc_regime:   str   = "ranging"
    breadth_pct:  float = 50.0
    breadth_bias: str   = "neutral"
    gainers:      int   = 0
    losers:       int   = 0
    flow_bias:    str   = "neutral"
    bias:         str   = "neutral"
    confidence:   int   = 0
    allow_long:   bool  = True
    allow_short:  bool  = True
    leverage_cap: int   = 10
    reason:       str   = ""

    def summary(self) -> str:
        gate = "SKIP ALL" if not (self.allow_long or self.allow_short) else "OPEN"
        return (
            f"Market | BTC: {self.btc_trend.upper()} ADX={self.btc_adx:.0f} ({self.btc_regime}) | "
            f"Breadth: {self.breadth_pct:.0f}% | Gate: {gate} | Lev cap: {self.leverage_cap}x"
        )


class MarketScanner:
    def __init__(self, exchange: ccxt.Exchange):
        self.exchange = exchange

    def _ema(self, values: np.ndarray, period: int) -> np.ndarray:
        ema    = np.zeros(len(values))
        ema[0] = values[0]
        k = 2 / (period + 1)
        for i in range(1, len(values)):
            ema[i] = values[i] * k + ema[i - 1] * (1 - k)
        return ema

    def _adx(self, highs, lows, closes, period=14) -> float:
        n = len(closes)
        if n < period + 2:
            return 20.0
        tr, pdm, mdm = np.zeros(n), np.zeros(n), np.zeros(n)
        for i in range(1, n):
            tr[i]  = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
            up, dn = highs[i] - highs[i-1], lows[i-1] - lows[i]
            pdm[i] = up if up > dn and up > 0 else 0
            mdm[i] = dn if dn > up and dn > 0 else 0
        atr = np.convolve(tr[1:],  np.ones(period) / period, mode="valid")
        pdi = np.convolve(pdm[1:], np.ones(period) / period, mode="valid") / atr * 100
        mdi = np.convolve(mdm[1:], np.ones(period) / period, mode="valid") / atr * 100
        dx  = np.abs(pdi - mdi) / (pdi + mdi + 1e-10) * 100
        adx = np.convolve(dx, np.ones(period) / period, mode="valid")
        return float(adx[-1]) if len(adx) > 0 else 20.0

    async def _scan_btc(self) -> dict:
        try:
            raw = await self.exchange.fetch_ohlcv("BTC/USDT:USDT", "4h", limit=200)
            df  = pd.DataFrame(raw, columns=["ts","open","high","low","close","volume"])
            closes, highs, lows = df["close"].values, df["high"].values, df["low"].values
            ema20  = self._ema(closes, 20)[-1]
            ema50  = self._ema(closes, 50)[-1]
            ema200 = self._ema(closes, 200)[-1]
            price  = closes[-1]
            adx    = self._adx(highs, lows, closes, 14)
            if ema20 > ema50 > ema200 and price > ema20:
                trend = "bull"
            elif ema20 < ema50 < ema200 and price < ema20:
                trend = "bear"
            else:
                trend = "neutral"
            return {"trend": trend, "adx": adx, "regime": "trending" if adx > 25 else "ranging"}
        except Exception as e:
            logger.warning("BTC scan failed: %s", e)
            return {"trend": "neutral", "adx": 20.0, "regime": "ranging"}

    async def _scan_breadth(self) -> dict:
        above, total, gainers, losers = 0, 0, 0, 0
        results = await asyncio.gather(*[self._check_coin(sym) for sym in config.SCAN_PAIRS],
                                       return_exceptions=True)
        for r in results:
            if isinstance(r, dict):
                total   += 1
                above   += 1 if r["above_ema50"] else 0
                gainers += 1 if r["change_24h"] > 2  else 0
                losers  += 1 if r["change_24h"] < -2 else 0
        if total == 0:
            return {"breadth_pct": 50.0, "breadth_bias": "neutral",
                    "gainers": 0, "losers": 0, "flow_bias": "neutral"}
        breadth_pct  = above / total * 100
        breadth_bias = "bull" if breadth_pct >= 60 else "bear" if breadth_pct <= 40 else "neutral"
        flow_pct     = gainers / total * 100
        flow_bias    = "bull" if flow_pct >= 55 else "bear" if flow_pct <= 35 else "neutral"
        return {"breadth_pct": breadth_pct, "breadth_bias": breadth_bias,
                "gainers": gainers, "losers": losers, "flow_bias": flow_bias}

    async def _check_coin(self, symbol: str) -> Optional[dict]:
        try:
            raw    = await self.exchange.fetch_ohlcv(symbol, "1h", limit=60)
            if not raw or len(raw) < 55:
                return None
            closes = np.array([r[4] for r in raw])
            ema50  = self._ema(closes, 50)[-1]
            ticker = await self.exchange.fetch_ticker(symbol)
            return {"above_ema50": closes[-1] > ema50,
                    "change_24h":  ticker.get("percentage", 0) or 0}
        except Exception:
            return None

    def _verdict(self, btc: dict, breadth: dict) -> MarketContext:
        ctx = MarketContext(
            btc_trend=btc["trend"], btc_adx=btc["adx"], btc_regime=btc["regime"],
            breadth_pct=breadth["breadth_pct"], breadth_bias=breadth["breadth_bias"],
            gainers=breadth["gainers"], losers=breadth["losers"], flow_bias=breadth["flow_bias"],
        )
        adx = btc["adx"]
        if adx > 35:
            ctx.allow_long = ctx.allow_short = False
            ctx.leverage_cap = 0
            ctx.confidence   = 0
            ctx.reason = f"ADX={adx:.0f} (extreme trend — skip all entries)."
        elif adx > 25:
            ctx.allow_long = ctx.allow_short = True
            ctx.leverage_cap = 5
            ctx.reason = f"ADX={adx:.0f} (moderate trend — capped at 5x)."
        else:
            ctx.allow_long = ctx.allow_short = True
            ctx.leverage_cap = 10
            ctx.reason = f"ADX={adx:.0f} (ranging — full leverage)."
        return ctx

    async def scan(self) -> MarketContext:
        btc, breadth = await asyncio.gather(self._scan_btc(), self._scan_breadth())
        ctx = self._verdict(btc, breadth)
        logger.info(ctx.summary())
        return ctx
