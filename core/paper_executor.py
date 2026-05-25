"""
paper_executor.py — Offline paper trading executor
"""
import csv, os, time, logging
from datetime import datetime, timezone
from core.order_executor import ActivePosition, DCALevel
from core.signal_engine import ValidatedSignal
import config

PAPER_TRADES_CSV = os.path.join("data", "paper_trades.csv")
_CSV_HEADERS = [
    "timestamp", "symbol", "direction", "score", "leverage",
    "avg_entry", "close_price", "fills", "cushion_used",
    "pnl_usd", "pnl_pct", "exit_reason", "hold_min",
]

def log_paper_trade(position: ActivePosition, close_price: float,
                    pnl: float, reason: str) -> None:
    os.makedirs("data", exist_ok=True)
    write_header = not os.path.exists(PAPER_TRADES_CSV)
    fills    = sum(1 for l in position.dca_levels if l.filled)
    notional = position.dca_margin * position.leverage or position.total_margin * position.leverage
    pnl_pct  = pnl / notional * 100 if notional else 0.0
    hold_min = round((time.time() - position.open_time) / 60, 1)
    row = {
        "timestamp":    datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
        "symbol":       position.symbol,
        "direction":    position.direction,
        "score":        position.validated.signal.score,
        "leverage":     position.leverage,
        "avg_entry":    round(position.avg_entry, 6),
        "close_price":  round(close_price, 6),
        "fills":        fills,
        "cushion_used": position.cushion_used,
        "pnl_usd":      round(pnl, 2),
        "pnl_pct":      round(pnl_pct, 2),
        "exit_reason":  reason,
        "hold_min":     hold_min,
    }
    with open(PAPER_TRADES_CSV, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_CSV_HEADERS)
        if write_header:
            writer.writeheader()
        writer.writerow(row)
    logging.getLogger(__name__).info(
        "[PAPER] %s %s | P&L $%.2f | %s", position.symbol, position.direction, pnl, reason
    )

logger = logging.getLogger(__name__)


class PaperExecutor:
    async def open_position(self, validated: ValidatedSignal):
        sym      = validated.signal.symbol
        dir      = validated.signal.direction
        lev      = validated.leverage
        e1_price = validated.signal.price

        levels = []
        for i, pct in enumerate(config.DCA_SPLITS):
            margin = config.ACTIVE_MARGIN * pct
            price  = (e1_price * ((1 - config.DCA_STEP_PCT / 100) ** i) if dir == "long"
                      else e1_price * ((1 + config.DCA_STEP_PCT / 100) ** i))
            notional  = margin * lev
            contracts = notional / price
            levels.append(DCALevel(
                entry_num=i + 1, pct=pct, margin=round(margin, 2),
                target_price=round(price, 6), contracts=round(contracts, 4),
                order_id=f"paper_e{i+1}",
            ))

        e1            = levels[0]
        e1.filled     = True
        e1.fill_price = e1_price
        e1.fill_time  = time.time()

        trail_stop = (e1_price * (1 - config.TRAIL_PCT / 100) if dir == "long"
                      else e1_price * (1 + config.TRAIL_PCT / 100))

        position = ActivePosition(
            symbol=sym, direction=dir, leverage=lev, validated=validated,
            dca_levels=levels, avg_entry=e1_price, total_contracts=e1.contracts,
            total_margin=e1.margin, peak_price=e1_price, trail_stop=trail_stop,
        )
        logger.info("[PAPER] Opened: %s %s | E1 @ $%.4f | Trail: $%.4f",
                    sym, dir, e1_price, trail_stop)
        return position

    async def close_position(self, position: ActivePosition, reason: str):
        position.is_open = False
        logger.info("[PAPER] Closed: %s | %s", position.symbol, reason)
        return {"average": None, "id": "paper_close"}

    async def cancel_pending_orders(self, position: ActivePosition):
        pass

    async def add_dca_fill(self, position: ActivePosition, level: DCALevel, fill_price: float):
        level.filled     = True
        level.fill_price = fill_price
        level.fill_time  = time.time()
        position.total_contracts += level.contracts
        position.total_margin    += level.margin
        filled         = [l for l in position.dca_levels if l.filled]
        total_notional = sum(l.fill_price * l.contracts for l in filled if l.fill_price)
        position.avg_entry = total_notional / position.total_contracts

    async def inject_reserve_margin(self, position: ActivePosition, amount: float):
        position.injections_used += 1
        return True
