"""
order_executor.py — Order placement engine
Places E1 market order + E2-E5 limit orders.
"""
import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional
import ccxt.async_support as ccxt
from core.signal_engine import ValidatedSignal
import config

logger = logging.getLogger(__name__)


@dataclass
class DCALevel:
    entry_num:    int
    pct:          float
    margin:       float
    target_price: float
    contracts:    float
    order_id:     Optional[str] = None
    filled:       bool = False
    fill_price:   Optional[float] = None
    fill_time:    Optional[float] = None


@dataclass
class ActivePosition:
    symbol:          str
    direction:       str
    leverage:        int
    validated:       ValidatedSignal
    dca_levels:      list[DCALevel] = field(default_factory=list)
    avg_entry:       float = 0.0
    entry_price:     float = 0.0
    total_contracts: float = 0.0
    total_margin:    float = 0.0
    dca_margin:      float = 0.0
    peak_price:      float = 0.0
    trail_stop:      float = 0.0
    sl_ref_price:    float = 0.0
    cushion_used:    int   = 0
    cushion_margin:  float = 0.0
    recovery_tp:     bool  = False
    injections_used: int   = 0
    confirm_count:   int   = 0
    structural_sl:   float = 0.0   # setup candle SL level
    open_time:       float = field(default_factory=time.time)
    is_open:         bool  = True


class OrderExecutor:
    def __init__(self, exchange: ccxt.Exchange):
        self.exchange = exchange

    async def _set_leverage(self, symbol: str, leverage: int) -> bool:
        for attempt in range(config.ORDER_RETRY_LIMIT):
            try:
                await self.exchange.set_leverage(leverage, symbol)
                return True
            except Exception as e:
                logger.warning("Set leverage attempt %d failed: %s", attempt + 1, e)
                await asyncio.sleep(2 ** attempt)
        return False

    async def _set_margin_mode(self, symbol: str) -> None:
        try:
            await self.exchange.set_margin_mode("isolated", symbol)
        except Exception:
            pass

    async def _place_market_order(self, symbol: str, direction: str,
                                   amount: float) -> Optional[dict]:
        side = "buy" if direction == "long" else "sell"
        for attempt in range(config.ORDER_RETRY_LIMIT):
            try:
                return await self.exchange.create_order(
                    symbol, "market", side, amount,
                    params={"positionSide": "BOTH"}
                )
            except Exception as e:
                logger.warning("Market order attempt %d failed: %s", attempt + 1, e)
                await asyncio.sleep(2 ** attempt)
        return None

    async def _place_limit_order(self, symbol: str, direction: str,
                                  amount: float, price: float) -> Optional[dict]:
        side = "buy" if direction == "long" else "sell"
        for attempt in range(config.ORDER_RETRY_LIMIT):
            try:
                return await self.exchange.create_order(
                    symbol, "limit", side, amount, price,
                    params={"positionSide": "BOTH", "timeInForce": "GTC"}
                )
            except Exception as e:
                logger.warning("Limit order attempt %d failed: %s", attempt + 1, e)
                await asyncio.sleep(2 ** attempt)
        return None

    def _calc_dca_levels(self, validated: ValidatedSignal) -> list[DCALevel]:
        levels = []
        e1_price = validated.signal.price
        for i, pct in enumerate(config.DCA_SPLITS):
            margin = config.ACTIVE_MARGIN * pct
            if validated.signal.direction == "long":
                price = e1_price * ((1 - config.DCA_STEP_PCT / 100) ** i)
            else:
                price = e1_price * ((1 + config.DCA_STEP_PCT / 100) ** i)
            notional  = margin * validated.leverage
            contracts = notional / price
            levels.append(DCALevel(
                entry_num=i + 1, pct=pct, margin=round(margin, 2),
                target_price=round(price, 6), contracts=round(contracts, 4),
            ))
        return levels

    async def open_position(self, validated: ValidatedSignal) -> Optional[ActivePosition]:
        sym = validated.signal.symbol
        dir = validated.signal.direction
        lev = validated.leverage

        await self._set_margin_mode(sym)
        if not await self._set_leverage(sym, lev):
            logger.error("Failed to set leverage for %s — aborting", sym)
            return None

        levels   = self._calc_dca_levels(validated)
        e1       = levels[0]
        e1_order = await self._place_market_order(sym, dir, e1.contracts)
        if not e1_order:
            return None

        fill_price = float(e1_order.get("average") or e1_order.get("price") or e1.target_price)
        e1.filled     = True
        e1.order_id   = e1_order.get("id")
        e1.fill_price = fill_price
        e1.fill_time  = time.time()

        struct_sl  = validated.signal.structural_sl
        if config.SL_USE_STRUCTURAL and struct_sl > 0:
            trail_stop = struct_sl
        else:
            trail_stop = (fill_price * (1 - config.TRAIL_PCT / 100) if dir == "long"
                          else fill_price * (1 + config.TRAIL_PCT / 100))

        position = ActivePosition(
            symbol=sym, direction=dir, leverage=lev, validated=validated,
            dca_levels=levels, avg_entry=fill_price, entry_price=fill_price,
            total_contracts=e1.contracts, total_margin=e1.margin,
            dca_margin=e1.margin, peak_price=fill_price,
            trail_stop=trail_stop, sl_ref_price=fill_price,
            structural_sl=validated.signal.structural_sl,
        )

        for level in levels[1:]:
            order = await self._place_limit_order(sym, dir, level.contracts, level.target_price)
            if order:
                level.order_id = order.get("id")
            await asyncio.sleep(0.2)

        logger.info("Position opened: %s %s | E1 fill: $%.4f | Trail: $%.4f",
                    sym, dir, fill_price, trail_stop)
        return position

    async def add_dca_fill(self, position: ActivePosition, level: DCALevel,
                            fill_price: float) -> None:
        level.filled     = True
        level.fill_price = fill_price
        level.fill_time  = time.time()
        position.total_contracts += level.contracts
        position.total_margin    += level.margin
        position.dca_margin      += level.margin
        filled         = [l for l in position.dca_levels if l.filled]
        total_notional = sum(l.fill_price * l.contracts for l in filled if l.fill_price)
        position.avg_entry = total_notional / position.total_contracts

    async def close_position(self, position: ActivePosition, reason: str) -> Optional[dict]:
        sym  = position.symbol
        side = "sell" if position.direction == "long" else "buy"
        for attempt in range(config.ORDER_RETRY_LIMIT):
            try:
                order = await self.exchange.create_order(
                    sym, "market", side, position.total_contracts,
                    params={"positionSide": "BOTH", "reduceOnly": True}
                )
                position.is_open = False
                await self.cancel_pending_orders(position)
                return order
            except Exception as e:
                logger.warning("Close attempt %d failed: %s", attempt + 1, e)
                await asyncio.sleep(2 ** attempt)
        logger.error("CRITICAL: Failed to close %s after %d attempts", sym, config.ORDER_RETRY_LIMIT)
        return None

    async def cancel_pending_orders(self, position: ActivePosition) -> None:
        for level in position.dca_levels:
            if not level.filled and level.order_id:
                try:
                    await self.exchange.cancel_order(level.order_id, position.symbol)
                except Exception as e:
                    logger.debug("Cancel order error: %s", e)

    async def inject_cushion_margin(self, position: ActivePosition,
                                     current_price: float) -> bool:
        if position.cushion_used >= config.CUSHION_TRANCHES:
            return False
        tranche = config.CUSHION_TOTAL / config.CUSHION_TRANCHES
        try:
            await self.exchange.add_margin(position.symbol, tranche,
                                           params={"positionSide": "BOTH"})
            position.sl_ref_price   = current_price
            position.peak_price     = current_price
            position.trail_stop     = (current_price * (1 - config.TRAIL_PCT / 100)
                                       if position.direction == "long"
                                       else current_price * (1 + config.TRAIL_PCT / 100))
            position.cushion_margin += tranche
            position.total_margin   += tranche
            position.cushion_used   += 1
            position.recovery_tp     = True
            logger.info("CUSHION INJECTED: %s | $%.0f | %d/%d used",
                        position.symbol, tranche, position.cushion_used, config.CUSHION_TRANCHES)
            return True
        except Exception as e:
            logger.error("Cushion injection failed for %s: %s", position.symbol, e)
            return False

    async def inject_reserve_margin(self, position: ActivePosition, amount: float) -> bool:
        position.injections_used += 1
        return True
