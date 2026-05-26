"""
MakStanleyz Scalping Bot — Configuration
All parameters in one place. Edit this file only.
Strategy: 1m entry (RSI-turn + vol spike) | 5m trend filter
"""
from dotenv import load_dotenv
import os

load_dotenv()

# ── API credentials ──────────────────────────────────────────
API_KEY     = os.getenv("BINANCE_API_KEY", "paper")
API_SECRET  = os.getenv("BINANCE_SECRET_KEY", "paper")
TESTNET     = os.getenv("TESTNET", "true").lower() == "true"
PAPER_MODE  = os.getenv("PAPER_MODE", "false").lower() == "true"

# ── Telegram ─────────────────────────────────────────────────
TG_TOKEN    = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT_ID  = os.getenv("TELEGRAM_CHAT_ID")

# ── Capital management ────────────────────────────────────────
MAX_POSITIONS     = 1       # 1 position at a time — full capital focus
TOTAL_ALLOCATION  = 300.0   # USDT — total capital (1 slot × $300)
_PER_SLOT         = TOTAL_ALLOCATION / MAX_POSITIONS   # $300 per slot
RESERVE_PCT       = 0.50    # 50% of each slot held as reserve
ACTIVE_MARGIN     = _PER_SLOT * (1 - RESERVE_PCT)     # $150 per position
RESERVE_MARGIN    = _PER_SLOT * RESERVE_PCT            # $150 per position
INJECT_TRANCHE    = RESERVE_MARGIN * 0.25              # $37.50 per injection
MAX_INJECTIONS    = 2       # maximum reserve injections per trade

# ── DCA ladder ───────────────────────────────────────────────
DCA_SPLITS        = [0.10, 0.15, 0.20, 0.25, 0.30]  # % of ACTIVE margin
DCA_STEP_PCT      = 0.8     # % between each DCA level (tight for 1m scalping)

# ── Signal thresholds ────────────────────────────────────────
RSI_OVERSOLD      = 20      # extreme oversold level for SETUP bar
RSI_OVERBOUGHT    = 80      # extreme overbought level for SETUP bar
VOL_SPIKE_MULT    = 4.0     # spike bar must be ≥4× avg vol (optimised: higher win rate vs 5.0)
MIN_SCORE         = 80      # only high-conviction setups
GAINER_THRESHOLD  = 10.0   # 24h % gain for top gainer short
LOSER_THRESHOLD   = -10.0  # 24h % loss for top loser long

# ── Hold engine ───────────────────────────────────────────────
RSI_HOLD_LONG     = 70      # exit long trigger: RSI crosses above
RSI_HOLD_SHORT    = 30      # exit short trigger: RSI crosses below
EMA_FAST          = 5
EMA_MID           = 8
EMA_SLOW          = 13
HTF_EMA           = 50      # 5m EMA for HTF bias
EXIT_CONFIRM_BARS = 1       # 1 bar confirm — 1m scalping

# ── Scalp profit targets ──────────────────────────────────────
TAKE_PROFIT_PCT   = 2.0    # arm tight trail when profit hits 2% (optimised for micro trading)
MAX_PROFIT_PCT    = 2.0    # hard exit at 2% — fast scalp capture
STOP_LOSS_PCT     = 1.5    # hard stop loss from initial entry
WITHDRAW_AT_PCT   = 4.0    # reclaim injected reserve at +4% profit

# ── Trail stop — tightens once in the profit zone ────────────
TRAIL_STEPS = [
    (1.0, 0.8),   # below 1% profit → 0.8% trail
    (2.0, 0.5),   # 1–2% → 0.5% trail (locks in profit once 1% hit)
    (999, 0.3),   # above 2% → 0.3% trail (ride any extra past TP)
]
TRAIL_PCT         = 1.5    # initial trail before any profit

# ── Cushion fund (margin dilution buffer) ────────────────────
CUSHION_PCT          = 0.20    # 20% of per-slot capital = $20
CUSHION_TOTAL        = _PER_SLOT * CUSHION_PCT         # $20 per slot
CUSHION_TRANCHES     = 2       # 2 injections of $10 each
CUSHION_RECOVERY_TP  = 2.0     # after any injection: exit at +2% profit

# ── Risk hard limits ─────────────────────────────────────────
MAX_LOSS_PER_TRADE    = 10.0    # USDT — absolute ceiling per trade
DAILY_LOSS_LIMIT      = 25.0    # USDT — stop trading for the day
INJECT_TRIGGER_PCT    = 5.0    # inject when liq is within 5%
INJECT_WARN_PCT       = 10.0   # warn when liq is within 10%

# ── Funding rate signal boost ─────────────────────────────────
FUND_RATE_STRONG  = -0.001    # < -0.1%  → +10 score pts for longs
FUND_RATE_MILD    = -0.0005   # < -0.05% → +5  score pts for longs

# ── Order book depth filter ────────────────────────────────────
OB_DEPTH          = 10        # top N bid/ask levels to measure
OB_LONG_MIN       = 0.80      # min bid/ask ratio to allow long (block ask-heavy books)
OB_SHORT_MAX      = 1.25      # max bid/ask ratio to allow short (block bid-heavy books)
OB_STRONG_LONG    = 1.50      # ratio ≥ 1.5 → +15 score pts (strong bid wall)
OB_MILD_LONG      = 1.20      # ratio ≥ 1.2 → +8 score pts
OB_STRONG_SHORT   = 0.67      # ratio ≤ 0.67 → +15 score pts (strong ask wall)
OB_MILD_SHORT     = 0.83      # ratio ≤ 0.83 → +8 score pts

# ── Dynamic top-movers selection ─────────────────────────────
TOP_MOVERS_COUNT    = 5
TOP_MOVER_MIN_VOL   = 5_000_000
TOP_MOVER_LEVERAGE  = 10

# ── Leverage decision table ──────────────────────────────────
LEVERAGE_TABLE = {
    (True,  True):  10,
    (True,  None):   9,
    (True,  False):  6,
    (None,  True):   7,
    (None,  None):   5,
    (None,  False):  4,
    (False, True):   4,
    (False, None):   3,
    (False, False):  2,
}

# ── Execution settings ────────────────────────────────────────
SCAN_INTERVAL_SEC            = 60      # signal scanner runs every 60s
MARKET_CONTEXT_INTERVAL_SEC  = 900     # BTC trend refresh (15 min)
POSITION_CHECK_SEC    = 10      # 10s — fast checks for 1m scalping
TIMEFRAME             = "1m"    # entry timeframe
HTF_TIMEFRAME         = "5m"    # 5m trend filter (HTF context)
BTC_TIMEFRAME         = "5m"    # BTC structure check
CANDLES_NEEDED        = 288     # 288 × 1m = 24 hours of history
_TF_MIN               = 1      # 1m bars
BARS_PER_24H          = 1440   # 1m bars per day
ORDER_RETRY_LIMIT     = 3
SLIPPAGE_TOLERANCE    = 0.002

# ── Pairs to scan ─────────────────────────────────────────────
SCAN_PAIRS = [
    "BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT",
    "BNB/USDT:USDT", "AVAX/USDT:USDT", "DOGE/USDT:USDT",
    "LINK/USDT:USDT", "ARB/USDT:USDT", "OP/USDT:USDT",
    "SUI/USDT:USDT", "INJ/USDT:USDT", "WIF/USDT:USDT",
    "APT/USDT:USDT", "NEAR/USDT:USDT",
    "JUP/USDT:USDT", "TIA/USDT:USDT", "ATOM/USDT:USDT",
    "S/USDT:USDT",   "RUNE/USDT:USDT", "LDO/USDT:USDT",
    "AAVE/USDT:USDT", "UNI/USDT:USDT", "GMX/USDT:USDT",
    "STX/USDT:USDT", "SEI/USDT:USDT", "PYTH/USDT:USDT",
    "GRT/USDT:USDT", "ENS/USDT:USDT", "CFX/USDT:USDT",
]

COIN_TREND_EMA        = 50    # 5m EMA period for HTF bias

# ── Logging ───────────────────────────────────────────────────
LOG_DIR           = "data/logs"
LOG_MAX_BYTES     = 10 * 1024 * 1024
LOG_BACKUP_COUNT  = 5

# ── Dashboard ─────────────────────────────────────────────────
DASHBOARD_PORT    = 8080
DASHBOARD_HOST    = "0.0.0.0"
