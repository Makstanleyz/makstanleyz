"""
telegram_bot.py — Telegram alert system for MakStanleyz
"""
import logging, aiohttp
from datetime import datetime, timezone
import config

logger = logging.getLogger(__name__)
BASE_URL = f"https://api.telegram.org/bot{config.TG_TOKEN}"

async def _send(text: str) -> bool:
    if not config.TG_TOKEN or not config.TG_CHAT_ID:
        return False
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(f"{BASE_URL}/sendMessage",
                json={"chat_id": config.TG_CHAT_ID, "text": text, "parse_mode": "HTML"},
                timeout=aiohttp.ClientTimeout(total=10)) as r:
                return r.status == 200
    except Exception as e:
        logger.warning("Telegram error: %s", e)
        return False

def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M UTC")

async def alert_signal_found(signal, validated) -> None:
    gem  = "GEM " if signal.is_gem else ""
    mode = "TESTNET " if config.TESTNET else ""
    htf5 = "5m aligned" if validated.htf_aligned else "5m counter" if validated.htf_aligned is False else "5m neutral"
    await _send(
        f"<b>{mode}MAKSTANLEYZ {gem}SIGNAL</b>\n"
        f"Pair: <b>{signal.symbol}</b> | Dir: <b>{signal.direction.upper()}</b>\n"
        f"Score: <b>{signal.score}/100</b> | RSI: {signal.rsi} | Vol: {signal.vol_ratio}x\n"
        f"24h: {signal.change_24h}% | Price: ${signal.price:,.4f}\n"
        f"{htf5} | BTC: {'confirms' if validated.btc_confirms else 'against' if validated.btc_confirms is False else 'neutral'}\n"
        f"<b>Leverage: {validated.leverage}x</b> | Active: ${config.ACTIVE_MARGIN:.0f} | Reserve: ${config.RESERVE_MARGIN:.0f}\n"
        f"TP: {config.MAX_PROFIT_PCT}% | SL: {config.STOP_LOSS_PCT}% | Trail: {config.TRAIL_PCT}%\n"
        f"<i>{_ts()}</i>"
    )

async def alert_e1_filled(position) -> None:
    e1 = position.dca_levels[0]
    await _send(
        f"<b>E1 FILLED — POSITION OPEN</b>\n"
        f"<b>{position.symbol}</b> {position.direction.upper()} x{position.leverage}\n"
        f"Fill: ${e1.fill_price:,.4f} | Margin: ${e1.margin:.2f} | Qty: {e1.contracts:.4f}\n"
        f"Trail stop: ${position.trail_stop:,.4f}\n"
        f"<i>{_ts()}</i>"
    )

async def alert_dca_filled(position, level, status: dict) -> None:
    await _send(
        f"<b>DCA E{level.entry_num} FILLED</b>\n"
        f"<b>{position.symbol}</b> | Fill: ${level.fill_price:,.4f}\n"
        f"Avg entry: ${position.avg_entry:,.4f} | P&L: {status['profit_pct']:+.1f}%\n"
        f"<i>{_ts()}</i>"
    )

async def alert_inject_urgent(position, liq_dist: float, amount: float) -> None:
    await _send(
        f"INJECT RESERVE NOW\n"
        f"<b>{position.symbol}</b> — Liq is <b>{liq_dist:.1f}% away</b>\n"
        f"Add <b>${amount:.0f}</b> margin on Binance\n"
        f"<i>{_ts()}</i>"
    )

async def alert_inject_warning(position, liq_dist: float) -> None:
    await _send(
        f"<b>RESERVE WARNING</b>\n"
        f"<b>{position.symbol}</b> — Liq is {liq_dist:.1f}% away\n"
        f"Prepare ${config.INJECT_TRANCHE:.0f} tranche\n"
        f"<i>{_ts()}</i>"
    )

async def alert_caution(position, flips: int, exit_eval: dict) -> None:
    await _send(
        f"<b>CAUTION {flips}/3 EXIT CONDITIONS FLIPPED</b>\n"
        f"<b>{position.symbol}</b> {position.direction.upper()}\n"
        f"RSI: {'HOLD' if exit_eval.get('rsi_holds') else 'FLIP'} | "
        f"EMA: {'HOLD' if exit_eval.get('ema_holds') else 'FLIP'} | "
        f"VWAP: {'HOLD' if exit_eval.get('vwap_holds') else 'FLIP'}\n"
        f"<i>{_ts()}</i>"
    )

async def alert_exit_fired(position, exit_eval: dict, pnl: float, close_price: float) -> None:
    result = "PROFIT" if pnl >= 0 else "LOSS"
    await _send(
        f"<b>CLOSED — {result}</b>\n"
        f"<b>{position.symbol}</b> {position.direction.upper()} | {exit_eval.get('exit_reason','')}\n"
        f"Exit: ${close_price:,.4f} | Avg entry: ${position.avg_entry:,.4f}\n"
        f"<b>P&L: ${pnl:+.2f}</b> | Fills: {sum(1 for l in position.dca_levels if l.filled)}/5\n"
        f"<i>{_ts()}</i>"
    )

async def alert_error(error: str) -> None:
    await _send(f"<b>MAKSTANLEYZ ERROR</b>\n{error}\n<i>{_ts()}</i>")

async def alert_heartbeat(uptime_hours: float, last_scan: str) -> None:
    mode = "TESTNET" if config.TESTNET else "LIVE"
    await _send(f"MakStanleyz alive ({mode}) | Uptime: {uptime_hours:.1f}h | Last scan: {last_scan}")
