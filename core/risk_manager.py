"""
risk_manager.py — Hard rule enforcement
"""
import logging
import time
from datetime import datetime, timezone
import config

logger = logging.getLogger(__name__)


class RiskManager:
    def __init__(self):
        self.daily_pnl:      float = 0.0
        self.daily_loss:     float = 0.0
        self.trades_today:   int   = 0
        self.open_positions: int   = 0
        self.session_date:   str   = self._today()
        self.trading_halted: bool  = False

    def _today(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _reset_daily_if_new_day(self):
        today = self._today()
        if today != self.session_date:
            self.daily_pnl      = 0.0
            self.daily_loss     = 0.0
            self.trades_today   = 0
            self.trading_halted = False
            self.session_date   = today

    def can_trade(self, score: int) -> tuple[bool, str]:
        self._reset_daily_if_new_day()
        if self.trading_halted:
            return False, f"Trading halted — daily loss limit ${config.DAILY_LOSS_LIMIT} reached"
        if self.open_positions >= config.MAX_POSITIONS:
            return False, f"Max positions ({config.MAX_POSITIONS}) already open"
        if score < config.MIN_SCORE:
            return False, f"Signal score {score} below minimum {config.MIN_SCORE}"
        if self.daily_loss >= config.DAILY_LOSS_LIMIT:
            self.trading_halted = True
            return False, f"Daily loss ${self.daily_loss:.2f} >= limit ${config.DAILY_LOSS_LIMIT}"
        return True, "All risk checks passed"

    def on_position_opened(self):
        self.open_positions += 1
        self.trades_today   += 1

    def on_position_closed(self, pnl: float):
        self.open_positions = max(0, self.open_positions - 1)
        self.daily_pnl     += pnl
        if pnl < 0:
            self.daily_loss += abs(pnl)
        if self.daily_loss >= config.DAILY_LOSS_LIMIT:
            self.trading_halted = True
            logger.warning("DAILY LOSS LIMIT: $%.2f — trading halted", self.daily_loss)

    def status(self) -> dict:
        self._reset_daily_if_new_day()
        return {
            "date":           self.session_date,
            "daily_pnl":      round(self.daily_pnl, 2),
            "daily_loss":     round(self.daily_loss, 2),
            "daily_limit":    config.DAILY_LOSS_LIMIT,
            "remaining_risk": round(config.DAILY_LOSS_LIMIT - self.daily_loss, 2),
            "trades_today":   self.trades_today,
            "open_positions": self.open_positions,
            "trading_halted": self.trading_halted,
        }
