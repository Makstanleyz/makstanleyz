#!/usr/bin/env python3
"""
backtest.py — MakStanleyz Backtester
1m entry + 5m trend filter | 3 simultaneous positions | $100/slot

Run:
  python3 backtest.py                         # 1m TF (default), 3 positions
  python3 backtest.py --timeframe 5m          # compare against 5m
  python3 backtest.py --timeframe all         # compare 1m / 5m / 15m
  python3 backtest.py --days 30               # 30-day lookback (recommended for 1m)
  python3 backtest.py --capital 100           # $100 per slot
  python3 backtest.py --no-htf-filter         # disable 5m trend filter
  python3 backtest.py --symbols BTCUSDT ETHUSDT

NOTE: 1m data is large — 90 days = ~129k bars/symbol. Use --days 14 to 30 for
      fast runs during initial testing.
"""
import asyncio
import argparse
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd
import numpy as np
import ccxt.async_support as ccxt
from dataclasses import dataclass, field
from typing import Optional
import config

# ── Per-timeframe parameter overrides ────────────────────────────────────────
TF_PARAMS = {
    "1m": {
        "bars_per_day":        1440,
        "tf_ms":               1 * 60 * 1000,
        "dca_step_pct":        0.8,
        "stop_loss_pct":       1.5,
        "trail_pct":           2.0,
        "trail_steps":         [(2.0, 1.5), (4.0, 1.0), (999, 0.5)],
        "max_profit_pct":      4.0,
        "cushion_recovery_tp": 2.0,
    },
    "3m": {
        "bars_per_day":        480,
        "tf_ms":               3 * 60 * 1000,
        "dca_step_pct":        1.0,
        "stop_loss_pct":       1.8,
        "trail_pct":           2.5,
        "trail_steps":         [(3.0, 2.0), (6.0, 1.0), (999, 0.5)],
        "max_profit_pct":      6.0,
        "cushion_recovery_tp": 2.5,
    },
    "5m": {
        "bars_per_day":        288,
        "tf_ms":               5 * 60 * 1000,
        "dca_step_pct":        1.5,
        "stop_loss_pct":       2.5,
        "trail_pct":           3.0,
        "trail_steps":         [(5.0, 3.0), (10.0, 1.5), (999, 1.0)],
        "max_profit_pct":      10.0,
        "cushion_recovery_tp": 4.0,
    },
    "15m": {
        "bars_per_day":        96,
        "tf_ms":               15 * 60 * 1000,
        "dca_step_pct":        2.0,
        "stop_loss_pct":       3.5,
        "trail_pct":           4.5,
        "trail_steps":         [(7.0, 4.5), (15.0, 2.0), (999, 1.5)],
        "max_profit_pct":      15.0,
        "cushion_recovery_tp": 6.0,
    },
}

# ── CLI args ──────────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser()
parser.add_argument("--timeframe",      default="1m",
                    choices=["all", "1m", "3m", "5m", "15m"],
                    help="Timeframe (default: 1m)")
parser.add_argument("--days",           type=int, default=30,
                    help="Lookback days (default: 30 — use ≤30 for 1m)")
parser.add_argument("--max-positions",  type=int, default=3, dest="max_positions")
parser.add_argument("--capital",        type=float, default=100.0,
                    help="Capital per slot USDT (default: 100)")
parser.add_argument("--leverage",       type=int, default=10)
parser.add_argument("--symbols",        nargs="*", default=None)
parser.add_argument("--no-htf-filter",  action="store_true", dest="no_htf_filter",
                    help="Disable 5m trend filter gate")
parser.add_argument("--vol-spike",      type=float, default=None, dest="vol_spike",
                    help="Override VOL_SPIKE_MULT (default: from config, 7.0)")
parser.add_argument("--min-score",      type=int,   default=None, dest="min_score",
                    help="Override MIN_SCORE (default: from config, 80)")
parser.add_argument("--rsi-os",         type=int,   default=None, dest="rsi_os",
                    help="Override RSI_OVERSOLD (default: 20)")
parser.add_argument("--rsi-ob",         type=int,   default=None, dest="rsi_ob",
                    help="Override RSI_OVERBOUGHT (default: 80)")
parser.add_argument("--same-bar-cap",   dest="same_bar_cap",
                    action=argparse.BooleanOptionalAction, default=True)
parser.add_argument("--btc-guard",      dest="btc_guard",
                    action=argparse.BooleanOptionalAction, default=True)
parser.add_argument("--htf-band",       type=float, default=None, dest="htf_band",
                    help="Override HTF neutral band % (default: 0.8). Try 1.5 or 2.0")
parser.add_argument("--tp",             type=float, default=None, dest="tp",
                    help="Override max_profit_pct / TAKE_PROFIT_PCT (e.g. 2.0)")
parser.add_argument("--macd-filter",    action="store_true", dest="macd_filter",
                    help="Require MACD histogram turning in signal direction")
parser.add_argument("--divergence",     action="store_true", dest="divergence",
                    help="Require RSI divergence confirmation")
parser.add_argument("--cvd-filter",     action="store_true", dest="cvd_filter",
                    help="Require CVD (volume delta) alignment with signal")
parser.add_argument("--structural-sl",  dest="structural_sl",
                    action=argparse.BooleanOptionalAction, default=True,
                    help="Anchor SL to setup candle extreme (default: on)")
parser.add_argument("--time-stop",      dest="time_stop",
                    action=argparse.BooleanOptionalAction, default=True,
                    help="Exit after N bars with no profit (default: on)")
args = parser.parse_args()

LEVERAGE        = args.leverage
HTF_FILTER_ON   = not args.no_htf_filter
HTF_BAND        = (args.htf_band / 100.0) if args.htf_band is not None else 0.008

# CLI overrides for signal params
if args.vol_spike is not None:
    config.VOL_SPIKE_MULT = args.vol_spike
if args.min_score is not None:
    config.MIN_SCORE = args.min_score
if args.rsi_os is not None:
    config.RSI_OVERSOLD = args.rsi_os
if args.rsi_ob is not None:
    config.RSI_OVERBOUGHT = args.rsi_ob
TEST_SYMBOLS    = (
    [s.replace("USDT", "") + "/USDT:USDT" for s in args.symbols]
    if args.symbols else config.SCAN_PAIRS
)
TIMEFRAMES = ["1m", "3m", "5m", "15m"] if args.timeframe == "all" else [args.timeframe]

PER_SLOT_CAPITAL = args.capital
ACTIVE_MARGIN    = PER_SLOT_CAPITAL * 0.50
RESERVE_MARGIN   = PER_SLOT_CAPITAL * 0.50
CUSHION_TOTAL    = PER_SLOT_CAPITAL * 0.20

# ── Indicator helpers ─────────────────────────────────────────────────────────
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

def calc_rsi_series(closes: np.ndarray, period: int = 14) -> np.ndarray:
    """Vectorized RSI series — O(n) instead of O(n²)."""
    rsi = np.full(len(closes), 50.0)
    if len(closes) < period + 1:
        return rsi
    deltas = np.diff(closes)
    gains  = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = gains[:period].mean()
    avg_loss = losses[:period].mean()
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            rsi[i + 1] = 100.0
        else:
            rs = avg_gain / avg_loss
            rsi[i + 1] = 100.0 - 100.0 / (1.0 + rs)
    return rsi

def calc_vol_ratio(volumes: np.ndarray, window: int = 20) -> float:
    if len(volumes) < window + 1:
        return 1.0
    avg = volumes[-(window + 1):-1].mean()
    return float(volumes[-1] / avg) if avg > 0 else 1.0

def calc_ema_series(values: np.ndarray, period: int) -> np.ndarray:
    ema    = np.zeros(len(values))
    ema[0] = values[0]
    k = 2 / (period + 1)
    for i in range(1, len(values)):
        ema[i] = values[i] * k + ema[i - 1] * (1 - k)
    return ema

def rolling_vwap(df: pd.DataFrame, window: int = 50) -> np.ndarray:
    hlc3   = (df["high"] + df["low"] + df["close"]) / 3
    result = np.full(len(df), np.nan)
    for i in range(window - 1, len(df)):
        sl      = slice(i - window + 1, i + 1)
        vol_sl  = df["volume"].values[sl]
        cum_vol = vol_sl.sum()
        if cum_vol > 0:
            result[i] = (hlc3.values[sl] * vol_sl).sum() / cum_vol
    return result

def calc_macd_histogram(closes: np.ndarray, fast=12, slow=26, signal=9) -> np.ndarray:
    if len(closes) < slow + signal:
        return np.zeros(len(closes))
    ema_fast = calc_ema_series(closes, fast)
    ema_slow = calc_ema_series(closes, slow)
    macd_line = ema_fast - ema_slow
    signal_line = calc_ema_series(macd_line, signal)
    return macd_line - signal_line

def calc_cvd(opens: np.ndarray, closes: np.ndarray, highs: np.ndarray,
             lows: np.ndarray, volumes: np.ndarray, window: int = 20) -> float:
    """Cumulative volume delta over last N bars. Positive = buying pressure."""
    if len(closes) < window:
        return 0.0
    sl = slice(-window, None)
    o, c, h, l, v = opens[sl], closes[sl], highs[sl], lows[sl], volumes[sl]
    ranges = np.where(h - l > 0, h - l, 1e-9)
    buy_pct = (c - l) / ranges
    delta   = (buy_pct * 2 - 1) * v
    return float(delta.sum())

def detect_rsi_divergence(closes: np.ndarray, rsi_series: np.ndarray,
                          direction: str, lookback: int = 30) -> bool:
    """
    Bullish divergence (long): price made lower low but RSI made higher low.
    Bearish divergence (short): price made higher high but RSI made lower high.
    """
    if len(closes) < lookback + 2 or len(rsi_series) < lookback + 2:
        return False
    c   = closes[-(lookback):]
    r   = rsi_series[-(lookback):]
    if direction == "long":
        price_low_now  = c[-1]
        price_low_prev = c[:-5].min()
        rsi_now        = r[-1]
        rsi_at_prev_low = r[np.argmin(c[:-5])]
        return price_low_now < price_low_prev and rsi_now > rsi_at_prev_low + 2
    else:
        price_hi_now   = c[-1]
        price_hi_prev  = c[:-5].max()
        rsi_now        = r[-1]
        rsi_at_prev_hi = r[np.argmax(c[:-5])]
        return price_hi_now > price_hi_prev and rsi_now < rsi_at_prev_hi - 2

def trail_pct_for(profit_pct: float, p: dict) -> float:
    for threshold, pct in p["trail_steps"]:
        if profit_pct < threshold:
            return pct
    return p["trail_steps"][-1][1]

def calc_bb(closes: np.ndarray, period: int = 20, std_mult: float = 2.0):
    """Returns (upper, mid, lower) Bollinger Bands for the last bar."""
    if len(closes) < period:
        return None, None, None
    sl   = closes[-period:]
    mid  = sl.mean()
    std  = sl.std()
    return mid + std_mult * std, mid, mid - std_mult * std

def calc_score(rsi: float, vol_ratio: float, change_24h: float, direction: str) -> int:
    score = 0
    if direction == "long":
        score += 40 if rsi < 20 else 25 if rsi < 25 else 10 if rsi < 30 else 0
        score += 30 if vol_ratio >= 5 else 15 if vol_ratio >= 3 else 0
        score += 20 if change_24h < -15 else 10 if change_24h < -8 else 5 if change_24h < -3 else 0
    else:
        score += 40 if rsi > 80 else 25 if rsi > 75 else 10 if rsi > 70 else 0
        score += 30 if vol_ratio >= 5 else 15 if vol_ratio >= 3 else 0
        score += 20 if change_24h > 15 else 10 if change_24h > 8 else 5 if change_24h > 3 else 0
    return min(score, 100)

def htf5_state_at(closes_5m: np.ndarray, idx_5m: int) -> int:
    """5m EMA50 vs price — 1=bull, -1=bear, 0=neutral (configurable band)."""
    if idx_5m < 50:
        return 0
    ema50 = calc_ema_series(closes_5m[:idx_5m + 1], 50)[-1]
    price = closes_5m[idx_5m]
    band  = HTF_BAND
    if price > ema50 * (1 + band):
        return 1
    if price < ema50 * (1 - band):
        return -1
    return 0

# ── Same-bar cap & BTC guard ──────────────────────────────────────────────────
@dataclass
class Trade:
    symbol:          str
    direction:       str
    score:           int
    entry_bar:       int
    entry_time:      str
    entry_price:     float
    leverage:        int
    dca_prices:      list
    dca_margins:     list
    dca_contracts:   list
    avg_entry:       float = 0.0
    total_margin:    float = 0.0
    total_contracts: float = 0.0
    peak_price:      float = 0.0
    trail_stop:      float = 0.0
    fills:           int   = 1
    exit_bar:        int   = 0
    exit_time:       str   = ""
    exit_price:      float = 0.0
    exit_reason:     str   = ""
    pnl:             float = 0.0
    pnl_pct:         float = 0.0
    hold_bars:       int   = 0
    dca_margin:         float = 0.0
    sl_ref_price:       float = 0.0
    structural_sl_price:float = 0.0
    cushion_used:       int   = 0
    cushion_margin:     float = 0.0
    recovery_tp:        bool  = False

def apply_position_limit(all_trades: list[Trade], max_pos: int) -> list[Trade]:
    if max_pos <= 0:
        return all_trades
    sorted_trades = sorted(all_trades, key=lambda t: t.entry_time)
    active_exits: list[str] = []
    selected: list[Trade]   = []
    for trade in sorted_trades:
        active_exits = [et for et in active_exits if et > trade.entry_time]
        if len(active_exits) < max_pos:
            selected.append(trade)
            active_exits.append(trade.exit_time or "9999-99-99")
    return selected

def apply_same_bar_cap(trades: list[Trade]) -> list[Trade]:
    by_bar: dict[str, Trade] = {}
    for t in trades:
        if t.entry_time not in by_bar or t.score > by_bar[t.entry_time].score:
            by_bar[t.entry_time] = t
    return list(by_bar.values())

def compute_btc_declining_set(df_btc: pd.DataFrame, bars_4h: int,
                               decline_pct: float = 3.0) -> set:
    closes     = df_btc["close"].values
    timestamps = df_btc["ts"].values
    declining  = set()
    for i in range(bars_4h, len(closes)):
        ret = (closes[i] - closes[i - bars_4h]) / closes[i - bars_4h] * 100
        if ret < -decline_pct:
            declining.add(str(timestamps[i])[:16])
    return declining

# ── Per-symbol simulation ─────────────────────────────────────────────────────
def simulate(df: pd.DataFrame, df_5m: Optional[pd.DataFrame], symbol: str, p: dict,
             btc_declining_set: set = None, htf_filter: bool = True) -> list[Trade]:
    closes  = df["close"].values
    highs   = df["high"].values
    lows    = df["low"].values
    volumes = df["volume"].values

    opens = df["open"].values
    ema5  = calc_ema_series(closes, 5)
    ema8  = calc_ema_series(closes, 8)
    ema13 = calc_ema_series(closes, 13)
    vwap  = rolling_vwap(df)
    macd_hist  = calc_macd_histogram(closes)
    rsi_series = calc_rsi_series(closes)

    # Build a 5m close array aligned to 1m bar timestamps (forward-fill)
    closes_5m  = None
    ts_5m      = None
    ts_1m      = df["ts"].values
    if htf_filter and df_5m is not None:
        closes_5m = df_5m["close"].values
        ts_5m     = df_5m["ts"].values

    trades: list[Trade]   = []
    active: Optional[Trade] = None
    confirm_count   = 0
    last_signal_bar = -999
    daily_loss      = 0.0
    last_date       = ""
    bars_per_day    = p["bars_per_day"]

    def _get_5m_idx(bar_idx: int) -> int:
        """Find the most recent 5m bar index that is ≤ current 1m timestamp."""
        if ts_5m is None or len(ts_5m) == 0:
            return -1
        t = ts_1m[bar_idx]
        # Binary search
        lo, hi = 0, len(ts_5m) - 1
        res = -1
        while lo <= hi:
            mid = (lo + hi) // 2
            if ts_5m[mid] <= t:
                res = mid
                lo  = mid + 1
            else:
                hi  = mid - 1
        return res

    for i in range(100, len(df)):
        bar_date = str(df["ts"].iloc[i])[:10]
        if bar_date != last_date:
            daily_loss = 0.0
            last_date  = bar_date

        if active:
            price = closes[i]

            # Trail stop advancement
            if active.direction == "long":
                profit_pct = (price - active.avg_entry) / active.avg_entry * 100
                tpct = trail_pct_for(profit_pct, p)
                if price > active.peak_price:
                    active.peak_price = price
                new_trail = active.peak_price * (1 - tpct / 100)
                if new_trail > active.trail_stop:
                    active.trail_stop = new_trail
            else:
                profit_pct = (active.avg_entry - price) / active.avg_entry * 100
                tpct = trail_pct_for(profit_pct, p)
                if price < active.peak_price:
                    active.peak_price = price
                new_trail = active.peak_price * (1 + tpct / 100)
                if new_trail < active.trail_stop:
                    active.trail_stop = new_trail

            # DCA fills
            for lvl in range(1, 5):
                if active.fills <= lvl:
                    target = active.dca_prices[lvl]
                    hit = ((active.direction == "long"  and lows[i]  <= target) or
                           (active.direction == "short" and highs[i] >= target))
                    if hit:
                        active.fills           += 1
                        active.total_contracts += active.dca_contracts[lvl]
                        active.total_margin    += active.dca_margins[lvl]
                        active.dca_margin      += active.dca_margins[lvl]
                        filled_notional = sum(
                            active.dca_prices[j] * active.dca_contracts[j]
                            for j in range(active.fills)
                        )
                        active.avg_entry = filled_notional / active.total_contracts

            # SL / cushion — structural SL takes priority when available
            if args.structural_sl and active.structural_sl_price > 0:
                sl_price = active.structural_sl_price
            else:
                sl_price = (active.sl_ref_price * (1 - p["stop_loss_pct"] / 100)
                            if active.direction == "long"
                            else active.sl_ref_price * (1 + p["stop_loss_pct"] / 100))
            sl_hit = ((active.direction == "long"  and lows[i]  <= sl_price) or
                      (active.direction == "short" and highs[i] >= sl_price))

            if sl_hit and active.cushion_used < config.CUSHION_TRANCHES:
                tranche               = CUSHION_TOTAL / config.CUSHION_TRANCHES
                active.cushion_margin += tranche
                active.total_margin   += tranche
                active.cushion_used   += 1
                active.recovery_tp     = True
                active.sl_ref_price    = sl_price
                active.structural_sl_price = 0.0   # fall back to fixed % after injection
                active.peak_price      = sl_price
                active.trail_stop      = (sl_price * (1 - p["trail_pct"] / 100)
                                          if active.direction == "long"
                                          else sl_price * (1 + p["trail_pct"] / 100))
                sl_hit = False

            tp_pct       = p["cushion_recovery_tp"] if active.recovery_tp else p["max_profit_pct"]
            max_tp_price = (active.avg_entry * (1 + tp_pct / 100) if active.direction == "long"
                            else active.avg_entry * (1 - tp_pct / 100))
            max_tp_hit   = ((active.direction == "long"  and highs[i] >= max_tp_price) or
                            (active.direction == "short" and lows[i]  <= max_tp_price))
            trail_hit    = ((active.direction == "long"  and lows[i]  <= active.trail_stop) or
                            (active.direction == "short" and highs[i] >= active.trail_stop))

            # Triple-confirm exit
            rsi_v = calc_rsi(closes[:i + 1])
            rsi_holds  = rsi_v < config.RSI_HOLD_LONG  if active.direction == "long" else rsi_v > config.RSI_HOLD_SHORT
            ema_holds  = ema5[i] > ema8[i] > ema13[i]  if active.direction == "long" else ema5[i] < ema8[i] < ema13[i]
            vwap_holds = (price > vwap[i] if not np.isnan(vwap[i]) else True) if active.direction == "long" else (price < vwap[i] if not np.isnan(vwap[i]) else True)
            all_flipped   = not rsi_holds and not ema_holds and not vwap_holds
            confirm_count = confirm_count + 1 if all_flipped else 0
            triple_confirm = confirm_count >= config.EXIT_CONFIRM_BARS

            bars_in_trade  = i - active.entry_bar
            time_stop_hit  = (args.time_stop
                              and bars_in_trade >= config.TIME_STOP_BARS
                              and profit_pct <= 0.0)

            if max_tp_hit:
                should_exit = True; exit_price = max_tp_price; exit_reason = "TAKE_PROFIT"
            elif sl_hit:
                should_exit = True; exit_price = sl_price;     exit_reason = "STOP_LOSS"
            elif trail_hit:
                should_exit = True; exit_price = active.trail_stop; exit_reason = "TRAIL_STOP"
            elif time_stop_hit:
                should_exit = True; exit_price = price;         exit_reason = "TIME_STOP"
            elif triple_confirm:
                should_exit = True; exit_price = price;         exit_reason = "TRIPLE_CONFIRM"
            else:
                should_exit = False; exit_price = price; exit_reason = ""

            if should_exit:
                pnl = ((exit_price - active.avg_entry) / active.avg_entry * active.dca_margin * LEVERAGE
                       if active.direction == "long"
                       else (active.avg_entry - exit_price) / active.avg_entry * active.dca_margin * LEVERAGE)
                active.exit_bar    = i
                active.exit_time   = str(df["ts"].iloc[i])[:16]
                active.exit_price  = round(exit_price, 6)
                active.exit_reason = exit_reason
                active.pnl         = round(pnl, 2)
                active.pnl_pct     = round(pnl / active.total_margin * 100, 2)
                active.hold_bars   = i - active.entry_bar
                daily_loss += abs(pnl) if pnl < 0 else 0
                trades.append(active)
                active = None; confirm_count = 0
            continue

        if daily_loss >= config.DAILY_LOSS_LIMIT:
            continue
        if i - last_signal_bar < 3:
            continue

        rsi_now  = calc_rsi(closes[:i + 1])
        rsi_prev = calc_rsi(closes[:i])
        vol_curr = calc_vol_ratio(volumes[:i + 1])
        vol_prev = calc_vol_ratio(volumes[:i])

        ref_24h    = i - bars_per_day
        change_24h = ((closes[i] - closes[ref_24h]) / closes[ref_24h] * 100
                      if ref_24h >= 0 and closes[ref_24h] > 0 else 0.0)

        # 5m trend filter
        htf5 = 0
        if htf_filter and closes_5m is not None:
            idx5 = _get_5m_idx(i)
            if idx5 >= 0:
                htf5 = htf5_state_at(closes_5m, idx5)

        direction = ""
        long_ok  = (rsi_prev < config.RSI_OVERSOLD
                    and vol_prev >= config.VOL_SPIKE_MULT
                    and rsi_now >= rsi_prev + 2.0
                    and closes[i] > (highs[i] + lows[i]) / 2
                    and vol_curr < vol_prev
                    and (not htf_filter or htf5 >= 0))
        short_ok = (rsi_prev > config.RSI_OVERBOUGHT
                    and vol_prev >= config.VOL_SPIKE_MULT
                    and rsi_now <= rsi_prev - 2.0
                    and closes[i] < (highs[i] + lows[i]) / 2
                    and vol_curr < vol_prev
                    and (not htf_filter or htf5 <= 0))

        if long_ok:
            direction = "long"
        if short_ok:
            direction = "short"
        if not direction:
            continue

        if direction == "long" and btc_declining_set:
            if str(df["ts"].iloc[i])[:16] in btc_declining_set:
                continue

        bb_up, _, bb_lo = calc_bb(closes[:i], 20, 2.0)

        score = calc_score(rsi_prev, vol_prev, change_24h, direction)
        if score < config.MIN_SCORE:
            continue

        # MACD histogram filter: histogram must be turning in signal direction
        if args.macd_filter:
            h_now  = macd_hist[i-1]
            h_prev = macd_hist[i-2]
            if direction == "long"  and not (h_now > h_prev):
                continue
            if direction == "short" and not (h_now < h_prev):
                continue

        # RSI divergence filter
        if args.divergence:
            if not detect_rsi_divergence(closes[:i], rsi_series[:i], direction):
                continue

        # CVD filter: volume delta must align with expected reversal
        if args.cvd_filter:
            cvd = calc_cvd(opens[:i], closes[:i], highs[:i], lows[:i], volumes[:i])
            if direction == "long"  and cvd > 0:
                continue
            if direction == "short" and cvd < 0:
                continue

        # Structural SL: below/above setup candle extreme, capped at SL_MAX_PCT
        if args.structural_sl:
            if direction == "long":
                raw_sl = lows[i - 1] * (1 - config.SL_STRUCTURAL_BUFF / 100)
                struct_sl = max(raw_sl, closes[i] * (1 - config.SL_MAX_PCT / 100))
            else:
                raw_sl = highs[i - 1] * (1 + config.SL_STRUCTURAL_BUFF / 100)
                struct_sl = min(raw_sl, closes[i] * (1 + config.SL_MAX_PCT / 100))
        else:
            struct_sl = 0.0

        e1 = closes[i]
        dca_prices, dca_margins, dca_contracts = [], [], []
        for idx, pct in enumerate(config.DCA_SPLITS):
            margin    = ACTIVE_MARGIN * pct
            price_lvl = (e1 * ((1 - p["dca_step_pct"] / 100) ** idx) if direction == "long"
                         else e1 * ((1 + p["dca_step_pct"] / 100) ** idx))
            notional  = margin * LEVERAGE
            contracts = notional / price_lvl
            dca_prices.append(round(price_lvl, 6))
            dca_margins.append(round(margin, 2))
            dca_contracts.append(round(contracts, 6))

        # Structural SL becomes the initial trail anchor — fires before standard trail
        if args.structural_sl and struct_sl > 0:
            trail_init = struct_sl
        else:
            trail_init = (e1 * (1 - p["trail_pct"] / 100) if direction == "long"
                          else e1 * (1 + p["trail_pct"] / 100))

        active = Trade(
            symbol=symbol, direction=direction, score=score,
            entry_bar=i, entry_time=str(df["ts"].iloc[i])[:16],
            entry_price=round(e1, 6), leverage=LEVERAGE,
            dca_prices=dca_prices, dca_margins=dca_margins, dca_contracts=dca_contracts,
            avg_entry=e1, total_margin=dca_margins[0], total_contracts=dca_contracts[0],
            peak_price=e1, trail_stop=trail_init,
            dca_margin=dca_margins[0], sl_ref_price=round(e1, 6),
            structural_sl_price=round(struct_sl, 6),
        )
        last_signal_bar = i

    if active:
        price = closes[-1]
        pnl = ((price - active.avg_entry) / active.avg_entry * active.dca_margin * LEVERAGE
               if active.direction == "long"
               else (active.avg_entry - price) / active.avg_entry * active.dca_margin * LEVERAGE)
        active.exit_price  = round(price, 6)
        active.exit_time   = str(df["ts"].iloc[-1])[:16]
        active.exit_reason = "OPEN_AT_END"
        active.pnl         = round(pnl, 2)
        active.pnl_pct     = round(pnl / active.total_margin * 100, 2)
        active.hold_bars   = len(df) - 1 - active.entry_bar
        trades.append(active)

    return trades

# ── Data fetching ─────────────────────────────────────────────────────────────
async def fetch_ohlcv(exchange, symbol: str, limit: int,
                      timeframe: str) -> Optional[pd.DataFrame]:
    tf_ms    = TF_PARAMS[timeframe]["tf_ms"]
    per_page = 1500
    try:
        all_bars = []
        since    = None
        while len(all_bars) < limit:
            fetch_n = min(per_page, limit - len(all_bars))
            raw = await exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=fetch_n)
            if not raw:
                break
            all_bars = raw + all_bars
            since    = raw[0][0] - per_page * tf_ms
            if len(raw) < fetch_n:
                break
            await asyncio.sleep(0.15)
        if len(all_bars) < 200:
            return None
        df = pd.DataFrame(all_bars, columns=["ts", "open", "high", "low", "close", "volume"])
        df = df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
        df["ts"] = pd.to_datetime(df["ts"], unit="ms")
        return df
    except Exception as e:
        print(f"  ✗ {symbol}: {e}")
        return None

# ── Results ───────────────────────────────────────────────────────────────────
def _stats(trades: list[Trade], label: str, bars_per_day: int) -> dict:
    if not trades:
        return {"label": label, "n": 0}
    wins     = [t for t in trades if t.pnl > 0]
    losses   = [t for t in trades if t.pnl <= 0]
    total    = sum(t.pnl for t in trades)
    win_sum  = sum(t.pnl for t in wins)
    loss_sum = abs(sum(t.pnl for t in losses))
    pf       = win_sum / loss_sum if loss_sum > 0 else float("inf")
    equity   = np.cumsum([t.pnl for t in trades])
    peak     = np.maximum.accumulate(equity)
    max_dd   = float((equity - peak).min())
    daily    = {}
    for t in trades:
        d = t.entry_time[:10]
        daily[d] = daily.get(d, 0) + t.pnl
    dpnl   = list(daily.values())
    sharpe = (np.mean(dpnl) / np.std(dpnl) * np.sqrt(252)
              if len(dpnl) > 1 and np.std(dpnl) > 0 else 0)
    return {
        "label": label, "n": len(trades),
        "wins": len(wins), "losses": len(losses),
        "win_pct": len(wins) / len(trades) * 100,
        "total_pnl": total, "profit_factor": pf, "max_dd": max_dd, "sharpe": sharpe,
        "avg_hold_hrs": np.mean([t.hold_bars for t in trades]) / (bars_per_day / 24),
        "avg_fills":    np.mean([t.fills for t in trades]),
        "sl_exits":        sum(1 for t in trades if t.exit_reason == "STOP_LOSS"),
        "tp_exits":        sum(1 for t in trades if t.exit_reason == "TAKE_PROFIT"),
        "trail_exits":     sum(1 for t in trades if t.exit_reason == "TRAIL_STOP"),
        "confirm_exits":   sum(1 for t in trades if t.exit_reason == "TRIPLE_CONFIRM"),
        "time_stop_exits": sum(1 for t in trades if t.exit_reason == "TIME_STOP"),
        "open_exits":      sum(1 for t in trades if t.exit_reason == "OPEN_AT_END"),
        "cushion_trades": sum(1 for t in trades if t.cushion_used > 0),
        "cushion_total":  sum(t.cushion_margin for t in trades),
    }

def print_results(trades: list[Trade], timeframe: str, max_pos: int,
                  per_slot: float) -> None:
    p = TF_PARAMS[timeframe]
    W = 110
    print("\n" + "═" * W)
    print(f"  MakStanleyz {timeframe.upper()} | {max_pos}-pos | "
          f"${per_slot:.0f}/slot (${per_slot * max_pos:.0f} total) | "
          f"Lev {LEVERAGE}x | SL {p['stop_loss_pct']}% | Trail {p['trail_pct']}% | "
          f"TP {p['max_profit_pct']}% | DCA {p['dca_step_pct']}% | "
          f"5m filter: {'ON' if HTF_FILTER_ON else 'OFF'}")
    print("═" * W)
    s = _stats(trades, timeframe, p["bars_per_day"])
    if s["n"] == 0:
        print("  No trades.")
    else:
        rows = [{
            "Symbol":  t.symbol.replace("/USDT:USDT",""),
            "Dir":     t.direction.upper(),
            "Score":   t.score,
            "Entry":   t.entry_time,
            "E $":     t.entry_price,
            "X $":     t.exit_price,
            "Fills":   t.fills,
            "Cushion": t.cushion_used,
            "Bars":    t.hold_bars,
            "P&L $":   t.pnl,
            "P&L %":   t.pnl_pct,
            "Exit":    t.exit_reason,
        } for t in trades]
        pd.set_option("display.width", 120)
        pd.set_option("display.float_format", lambda x: f"{x:.2f}")
        print(pd.DataFrame(rows).to_string(index=False))
        print(f"\n  Trades: {s['n']} | Win: {s['win_pct']:.1f}% ({s['wins']}W/{s['losses']}L) | "
              f"P&L: ${s['total_pnl']:.2f} | PF: {s['profit_factor']:.2f} | "
              f"MaxDD: ${s['max_dd']:.2f} | Sharpe: {s['sharpe']:.2f}")
        print(f"  Avg hold: {s['avg_hold_hrs']:.1f}h | "
              f"TP={s['tp_exits']} SL={s['sl_exits']} Trail={s['trail_exits']} "
              f"TimeStop={s['time_stop_exits']} Confirm={s['confirm_exits']} Open={s['open_exits']}")
        print(f"  Cushion: {s['cushion_trades']} trades | ${s['cushion_total']:.0f} injected")
    print("═" * W)

# ── Main ──────────────────────────────────────────────────────────────────────
async def main():
    exchange = ccxt.binance({"options": {"defaultType": "future"}, "sandbox": False})

    print(f"\nMakStanleyz — Backtester")
    print(f"TFs: {TIMEFRAMES} | Symbols: {len(TEST_SYMBOLS)} | "
          f"Max positions: {args.max_positions} | Capital: ${PER_SLOT_CAPITAL:.0f}/slot | "
          f"Lev: {LEVERAGE}x | Lookback: {args.days} days | "
          f"5m filter: {'ON' if HTF_FILTER_ON else 'OFF'} | "
          f"Same-bar cap: {'ON' if args.same_bar_cap else 'OFF'} | "
          f"BTC guard: {'ON' if args.btc_guard else 'OFF'}\n")

    all_results: dict[str, list[Trade]] = {}

    for tf in TIMEFRAMES:
        p       = dict(TF_PARAMS[tf])  # copy so overrides don't persist
        if args.tp is not None:
            p["max_profit_pct"] = args.tp
        tf_bars = args.days * p["bars_per_day"]
        print(f"── {tf} ({tf_bars} bars / {args.days} days) ──")

        btc_declining_set = None
        if args.btc_guard:
            print("  BTC (guard)...", end=" ", flush=True)
            df_btc = await fetch_ohlcv(exchange, "BTC/USDT:USDT", tf_bars, tf)
            if df_btc is not None:
                bars_4h = round(4 * 3600 * 1000 / p["tf_ms"])
                btc_declining_set = compute_btc_declining_set(df_btc, bars_4h)
                print(f"{len(btc_declining_set)} declining bars")
            else:
                print("skipped")

        raw_trades: list[Trade] = []
        for symbol in TEST_SYMBOLS:
            short = symbol.replace("/USDT:USDT", "")
            print(f"  {short}...", end=" ", flush=True)
            df_1m = await fetch_ohlcv(exchange, symbol, tf_bars, tf)
            if df_1m is None:
                print("skipped")
                continue

            # Fetch 5m data for HTF filter (only when running 1m)
            df_5m = None
            if tf == "1m" and HTF_FILTER_ON:
                bars_5m = args.days * TF_PARAMS["5m"]["bars_per_day"]
                df_5m   = await fetch_ohlcv(exchange, symbol, bars_5m, "5m")

            trades = simulate(df_1m, df_5m, symbol, p, btc_declining_set, HTF_FILTER_ON)
            print(f"{len(df_1m)} bars → {len(trades)} signals")
            raw_trades.extend(trades)

        before_cap = len(raw_trades)
        if args.same_bar_cap:
            raw_trades  = apply_same_bar_cap(raw_trades)
            cap_blocked = before_cap - len(raw_trades)
        else:
            cap_blocked = 0

        limited = apply_position_limit(raw_trades, args.max_positions)
        skipped = len(raw_trades) - len(limited)
        cap_str = f" | -{cap_blocked} same-bar" if cap_blocked else ""
        print(f"  {before_cap} raw → {len(raw_trades)} after cap{cap_str} → "
              f"{len(limited)} selected (+{skipped} blocked by {args.max_positions}-pos limit)\n")

        all_results[tf] = limited

    await exchange.close()

    for tf in TIMEFRAMES:
        print_results(all_results[tf], tf, args.max_positions, PER_SLOT_CAPITAL)

if __name__ == "__main__":
    asyncio.run(main())
