"""
position_manager.py — Live position monitor (10s tick for 1m scalping)
"""
import logging
import time
import numpy as np
import ccxt.async_support as ccxt
from core.order_executor import ActivePosition, DCALevel
import config

logger = logging.getLogger(__name__)


class PositionManager:
    def __init__(self, exchange: ccxt.Exchange):
        self.exchange = exchange

    async def _fetch_current_price(self, symbol: str) -> float:
        ticker = await self.exchange.fetch_ticker(symbol)
        return float(ticker.get("last", 0))

    async def _fetch_position_info(self, symbol: str) -> dict:
        try:
            positions = await self.exchange.fetch_positions([symbol])
            for p in positions:
                if p.get("symbol") == symbol and float(p.get("contracts", 0)) > 0:
                    return p
        except Exception as e:
            logger.debug("fetch_positions error: %s", e)
        return {}

    def _check_dca_fills(self, position: ActivePosition, current_price: float) -> list[DCALevel]:
        newly_filled = []
        for level in position.dca_levels:
            if level.filled or level.order_id is None:
                continue
            if position.direction == "long"  and current_price <= level.target_price:
                newly_filled.append(level)
            elif position.direction == "short" and current_price >= level.target_price:
                newly_filled.append(level)
        return newly_filled

    def _trail_pct(self, profit_pct: float) -> float:
        for threshold, pct in config.TRAIL_STEPS:
            if profit_pct < threshold:
                return pct
        return config.TRAIL_STEPS[-1][1]

    def _update_trail_stop(self, position: ActivePosition, current_price: float) -> bool:
        profit_pct = self._calc_profit_pct(position, current_price)
        trail_pct  = self._trail_pct(profit_pct)
        if position.direction == "long":
            if current_price > position.peak_price:
                position.peak_price = current_price
            new_trail = position.peak_price * (1 - trail_pct / 100)
            if new_trail > position.trail_stop:
                position.trail_stop = new_trail
                return True
        else:
            if current_price < position.peak_price:
                position.peak_price = current_price
            new_trail = position.peak_price * (1 + trail_pct / 100)
            if new_trail < position.trail_stop:
                position.trail_stop = new_trail
                return True
        return False

    def _calc_liq_distance_pct(self, current_price: float, liq_price: float,
                                direction: str) -> float:
        if liq_price <= 0 or current_price <= 0:
            return 100.0
        if direction == "long":
            return (current_price - liq_price) / current_price * 100
        return (liq_price - current_price) / current_price * 100

    def _calc_profit_pct(self, position: ActivePosition, current_price: float) -> float:
        if position.avg_entry <= 0:
            return 0.0
        if position.direction == "long":
            return (current_price - position.avg_entry) / position.avg_entry * 100
        return (position.avg_entry - current_price) / position.avg_entry * 100

    async def monitor_tick(self, position: ActivePosition) -> dict:
        current_price  = await self._fetch_current_price(position.symbol)
        position_info  = await self._fetch_position_info(position.symbol)
        liq_price      = float(position_info.get("liquidationPrice", 0))

        liq_dist       = self._calc_liq_distance_pct(current_price, liq_price, position.direction)
        profit_pct     = self._calc_profit_pct(position, current_price)
        trail_updated  = self._update_trail_stop(position, current_price)
        newly_filled   = self._check_dca_fills(position, current_price)

        trail_hit = (
            position.direction == "long"  and current_price <= position.trail_stop or
            position.direction == "short" and current_price >= position.trail_stop
        )

        # Structural SL takes priority; falls back to fixed % if not set
        if config.SL_USE_STRUCTURAL and position.structural_sl > 0:
            sl_price = position.structural_sl
        else:
            sl_ref   = position.sl_ref_price if position.sl_ref_price > 0 else current_price
            sl_price = (sl_ref * (1 - config.STOP_LOSS_PCT / 100)
                        if position.direction == "long"
                        else sl_ref * (1 + config.STOP_LOSS_PCT / 100))

        time_stop_hit = (
            time.time() - position.open_time > config.TIME_STOP_BARS * 60
            and profit_pct <= 0.0
        )

        cushion_inject_ready = (
            position.cushion_used < config.CUSHION_TRANCHES and
            ((position.direction == "long"  and current_price <= sl_price) or
             (position.direction == "short" and current_price >= sl_price))
        )
        hard_sl_hit = (
            position.cushion_used >= config.CUSHION_TRANCHES and
            ((position.direction == "long"  and current_price <= sl_price) or
             (position.direction == "short" and current_price >= sl_price))
        )
        active_tp_pct = config.CUSHION_RECOVERY_TP if position.recovery_tp else config.MAX_PROFIT_PCT

        return {
            "current_price":        current_price,
            "liq_price":            liq_price,
            "liq_dist_pct":         round(liq_dist, 2),
            "profit_pct":           round(profit_pct, 2),
            "trail_stop":           position.trail_stop,
            "trail_hit":            trail_hit,
            "trail_updated":        trail_updated,
            "newly_filled":         newly_filled,
            "time_stop_hit":        time_stop_hit,
            "inject_urgent":        liq_dist <= config.INJECT_TRIGGER_PCT,
            "inject_warn":          liq_dist <= config.INJECT_WARN_PCT,
            "withdraw_ready":       profit_pct >= config.WITHDRAW_AT_PCT and position.injections_used > 0,
            "fills_count":          sum(1 for l in position.dca_levels if l.filled),
            "cushion_inject_ready": cushion_inject_ready,
            "hard_sl_hit":          hard_sl_hit,
            "active_tp_pct":        active_tp_pct,
            "cushion_used":         position.cushion_used,
        }
