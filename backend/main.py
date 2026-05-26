"""
MakStanleyz Scalp Bot — FastAPI Backend
Handles: Binance Futures API signing, order management, WebSocket price feeds,
         position monitoring, TP/SL automation, strategy signals.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import time
from contextlib import asynccontextmanager
from typing import Optional

import httpx
import websockets
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("makstanleyz")

# ── Config (loaded from config.json or env) ───────────────────────────────
import os, pathlib

CONFIG_PATH = pathlib.Path(__file__).parent / "config.json"

def load_config():
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            return json.load(f)
    return {
        "api_key": os.getenv("BINANCE_API_KEY", ""),
        "api_secret": os.getenv("BINANCE_API_SECRET", ""),
        "testnet": os.getenv("TESTNET", "true").lower() == "true",
        "leverage": int(os.getenv("LEVERAGE", "5")),
        "trade_investment": float(os.getenv("TRADE_INVESTMENT", "50")),
        "stop_loss_pct": float(os.getenv("STOP_LOSS_PCT", "1.5")),
        "take_profit_pct": float(os.getenv("TAKE_PROFIT_PCT", "3.0")),
        "scan_interval_sec": int(os.getenv("SCAN_INTERVAL", "4")),
        "max_open_positions": int(os.getenv("MAX_OPEN_POSITIONS", "5")),
        "signal_threshold": int(os.getenv("SIGNAL_THRESHOLD", "65")),
        "bot_running": False,
        "active_strategies": ["ema", "rsi", "bb", "vwap", "obf", "brk", "mom"],
    }

cfg = load_config()

# ── Binance endpoints ──────────────────────────────────────────────────────
LIVE_BASE    = "https://fapi.binance.com"
TESTNET_BASE = "https://testnet.binancefuture.com"
LIVE_WS      = "wss://fstream.binance.com"
TESTNET_WS   = "wss://stream.binancefuture.com"

def base_url():    return TESTNET_BASE if cfg["testnet"] else LIVE_BASE
def ws_base():     return TESTNET_WS   if cfg["testnet"] else LIVE_WS

# ── HMAC signing ──────────────────────────────────────────────────────────
def sign(params: dict) -> dict:
    params["timestamp"] = int(time.time() * 1000)
    query = "&".join(f"{k}={v}" for k, v in params.items())
    sig = hmac.new(
        cfg["api_secret"].encode("utf-8"),
        query.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()
    params["signature"] = sig
    return params

def auth_headers() -> dict:
    return {"X-MBX-APIKEY": cfg["api_key"]}

# ── State ──────────────────────────────────────────────────────────────────
open_positions: dict[str, dict] = {}   # positionId -> position info
trade_history:  list[dict]      = []   # closed trades
stats = {
    "total_pnl": 0.0,
    "today_pnl": 0.0,
    "wins": 0,
    "losses": 0,
    "total_trades": 0,
    "balance": 0.0,
    "best_trade": 0.0,
}
ws_clients: list[WebSocket] = []       # connected dashboard WebSocket clients
price_cache: dict[str, float] = {}     # latest prices

# ── WebSocket broadcast ────────────────────────────────────────────────────
async def broadcast(event: str, data: dict):
    msg = json.dumps({"event": event, "data": data})
    dead = []
    for ws in ws_clients:
        try:
            await ws.send_text(msg)
        except Exception:
            dead.append(ws)
    for d in dead:
        ws_clients.remove(d)

# ── Binance REST helpers ───────────────────────────────────────────────────
async def binance_get(path: str, params: dict = None, signed: bool = False):
    async with httpx.AsyncClient(timeout=10) as client:
        p = sign(params or {}) if signed else (params or {})
        r = await client.get(f"{base_url()}{path}", params=p, headers=auth_headers() if signed else {})
        r.raise_for_status()
        return r.json()

async def binance_post(path: str, params: dict):
    async with httpx.AsyncClient(timeout=10) as client:
        p = sign(params)
        r = await client.post(f"{base_url()}{path}", data=p, headers=auth_headers())
        r.raise_for_status()
        return r.json()

async def binance_delete(path: str, params: dict):
    async with httpx.AsyncClient(timeout=10) as client:
        p = sign(params)
        r = await client.delete(f"{base_url()}{path}", params=p, headers=auth_headers())
        r.raise_for_status()
        return r.json()

# ── Account helpers ────────────────────────────────────────────────────────
async def get_account_balance() -> float:
    try:
        data = await binance_get("/fapi/v2/balance", signed=True)
        for asset in data:
            if asset.get("asset") == "USDT":
                return float(asset.get("availableBalance", 0))
    except Exception as e:
        log.error(f"Balance fetch failed: {e}")
    return 0.0

async def set_leverage(symbol: str, leverage: int):
    try:
        await binance_post("/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage})
    except Exception as e:
        log.warning(f"Leverage set failed for {symbol}: {e}")

async def get_price(symbol: str) -> float:
    try:
        data = await binance_get("/fapi/v1/ticker/price", {"symbol": symbol})
        return float(data["price"])
    except Exception:
        return price_cache.get(symbol, 0.0)

# ── Signal Engine ──────────────────────────────────────────────────────────
STRATEGIES = {
    "ema":  {"name": "Scalp EMA Cross",       "weight": {"ema": 0.5, "vol": 0.3, "mom": 0.2}},
    "rsi":  {"name": "RSI Divergence",         "weight": {"rsi": 0.5, "vol": 0.3, "mom": 0.2}},
    "bb":   {"name": "Bollinger Squeeze",      "weight": {"bb":  0.5, "vol": 0.3, "ema": 0.2}},
    "vwap": {"name": "VWAP Reversion",         "weight": {"rsi": 0.3, "vol": 0.4, "mom": 0.3}},
    "obf":  {"name": "Order Flow Imbalance",   "weight": {"vol": 0.5, "mom": 0.3, "ema": 0.2}},
    "brk":  {"name": "Breakout Hunter",        "weight": {"bb":  0.4, "mom": 0.4, "vol": 0.2}},
    "mom":  {"name": "Momentum Ride",          "weight": {"mom": 0.5, "ema": 0.3, "vol": 0.2}},
}

async def compute_signal(symbol: str, strategy_id: str) -> dict:
    """
    Fetch real kline data from Binance and compute technical indicators.
    Returns signal score 0-100 and recommended side (LONG/SHORT).
    """
    try:
        klines = await binance_get("/fapi/v1/klines", {
            "symbol": symbol, "interval": "1m", "limit": 50
        })
        closes = [float(k[4]) for k in klines]
        volumes = [float(k[5]) for k in klines]

        if len(closes) < 21:
            return {"score": 0, "side": "LONG", "reason": "Insufficient data"}

        # ── EMA signal ──
        def ema(data, period):
            k = 2 / (period + 1)
            e = data[0]
            for v in data[1:]:
                e = v * k + e * (1 - k)
            return e

        ema9  = ema(closes[-9:],  9)
        ema21 = ema(closes[-21:], 21)
        ema_bull = ema9 > ema21
        ema_score = 70 + (abs(ema9 - ema21) / ema21 * 1000) if ema_bull else 30 - (abs(ema9 - ema21) / ema21 * 500)
        ema_score = max(0, min(100, ema_score))

        # ── RSI signal ──
        gains, losses = [], []
        for i in range(1, 15):
            d = closes[-15+i] - closes[-15+i-1]
            (gains if d > 0 else losses).append(abs(d))
        avg_gain = sum(gains)/14 if gains else 0.001
        avg_loss = sum(losses)/14 if losses else 0.001
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        rsi_score = (100 - rsi) if rsi > 50 else rsi  # distance from extremes
        rsi_bull = rsi < 45

        # ── Volume signal ──
        avg_vol = sum(volumes[-20:]) / 20
        cur_vol = volumes[-1]
        vol_ratio = cur_vol / avg_vol if avg_vol > 0 else 1
        vol_score = min(100, 50 + (vol_ratio - 1) * 50)

        # ── Momentum signal ──
        mom = (closes[-1] - closes[-5]) / closes[-5] * 100
        mom_bull = mom > 0
        mom_score = min(100, 50 + abs(mom) * 20)

        # ── Bollinger band signal ──
        sma20 = sum(closes[-20:]) / 20
        std20 = (sum((c - sma20)**2 for c in closes[-20:]) / 20) ** 0.5
        upper = sma20 + 2 * std20
        lower = sma20 - 2 * std20
        price = closes[-1]
        bb_pos = (price - lower) / (upper - lower) if upper != lower else 0.5
        bb_bull = bb_pos < 0.35
        bb_score = (1 - bb_pos) * 100 if bb_bull else bb_pos * 100

        indicators = {"ema": ema_score, "rsi": rsi_score, "vol": vol_score, "mom": mom_score, "bb": bb_score}

        strat = STRATEGIES.get(strategy_id, STRATEGIES["ema"])
        weights = strat["weight"]
        composite = sum(indicators.get(k, 50) * w for k, w in weights.items())

        # Determine direction by majority vote
        bulls = sum([ema_bull, rsi_bull, mom_bull, bb_bull])
        side = "LONG" if bulls >= 2 else "SHORT"

        return {
            "score": round(composite, 1),
            "side": side,
            "indicators": indicators,
            "strategy": strat["name"],
            "rsi": round(rsi, 1),
            "ema_cross": "BULL" if ema_bull else "BEAR",
        }
    except Exception as e:
        log.error(f"Signal error {symbol}: {e}")
        return {"score": 0, "side": "LONG", "reason": str(e)}

# ── Order placement ────────────────────────────────────────────────────────
async def place_order(symbol: str, side: str, usdt_amount: float, leverage: int) -> dict:
    """Place a market order. Returns order details or raises on failure."""
    await set_leverage(symbol, leverage)

    price = await get_price(symbol)
    if price == 0:
        raise ValueError(f"Could not get price for {symbol}")

    # Get symbol info for quantity precision
    try:
        info = await binance_get("/fapi/v1/exchangeInfo")
        sym_info = next((s for s in info["symbols"] if s["symbol"] == symbol), None)
        step_size = 0.001
        if sym_info:
            for f in sym_info.get("filters", []):
                if f["filterType"] == "LOT_SIZE":
                    step_size = float(f["stepSize"])
                    break
    except Exception:
        step_size = 0.001

    raw_qty = (usdt_amount * leverage) / price
    precision = len(str(step_size).rstrip("0").split(".")[-1]) if "." in str(step_size) else 0
    qty = round(raw_qty - (raw_qty % step_size), precision)
    if qty <= 0:
        raise ValueError(f"Quantity too small: {qty} for {symbol}")

    order = await binance_post("/fapi/v1/order", {
        "symbol":    symbol,
        "side":      "BUY" if side == "LONG" else "SELL",
        "type":      "MARKET",
        "quantity":  str(qty),
        "positionSide": "BOTH",
    })
    return {**order, "entry_price": price, "quantity": qty, "side": side}

async def place_tp_sl(symbol: str, side: str, qty: float, entry_price: float,
                       tp_pct: float, sl_pct: float):
    """Place Take-Profit and Stop-Loss orders."""
    close_side = "SELL" if side == "LONG" else "BUY"
    if side == "LONG":
        tp_price = round(entry_price * (1 + tp_pct / 100), 4)
        sl_price = round(entry_price * (1 - sl_pct / 100), 4)
    else:
        tp_price = round(entry_price * (1 - tp_pct / 100), 4)
        sl_price = round(entry_price * (1 + sl_pct / 100), 4)

    try:
        await binance_post("/fapi/v1/order", {
            "symbol":       symbol,
            "side":         close_side,
            "type":         "TAKE_PROFIT_MARKET",
            "stopPrice":    str(tp_price),
            "closePosition":"true",
            "timeInForce":  "GTE_GTC",
            "positionSide": "BOTH",
        })
    except Exception as e:
        log.warning(f"TP order failed {symbol}: {e}")

    try:
        await binance_post("/fapi/v1/order", {
            "symbol":       symbol,
            "side":         close_side,
            "type":         "STOP_MARKET",
            "stopPrice":    str(sl_price),
            "closePosition":"true",
            "timeInForce":  "GTE_GTC",
            "positionSide": "BOTH",
        })
    except Exception as e:
        log.warning(f"SL order failed {symbol}: {e}")

async def cancel_all_orders(symbol: str):
    try:
        await binance_delete("/fapi/v1/allOpenOrders", {"symbol": symbol})
    except Exception as e:
        log.warning(f"Cancel orders failed {symbol}: {e}")

# ── Position monitor ───────────────────────────────────────────────────────
async def monitor_positions():
    """Periodically sync open positions with Binance."""
    while True:
        try:
            if cfg.get("bot_running") and cfg.get("api_key"):
                data = await binance_get("/fapi/v2/positionRisk", signed=True)
                live = {d["symbol"]: d for d in data if float(d.get("positionAmt", 0)) != 0}

                # Update unrealised PnL
                for sym, pos in live.items():
                    upnl = float(pos.get("unRealizedProfit", 0))
                    if sym in open_positions:
                        open_positions[sym]["unrealised_pnl"] = round(upnl, 2)
                        open_positions[sym]["current_price"]  = float(pos.get("markPrice", 0))

                # Detect closed positions (no longer in Binance live list)
                closed_syms = [s for s in list(open_positions.keys()) if s not in live]
                for sym in closed_syms:
                    pos = open_positions.pop(sym)
                    exit_price = await get_price(sym)
                    ep = pos.get("entry_price", exit_price)
                    qty = pos.get("quantity", 0)
                    side = pos.get("side", "LONG")
                    raw_pnl = (exit_price - ep) * qty * (1 if side == "LONG" else -1)
                    win = raw_pnl > 0
                    trade = {
                        "id": pos["id"],
                        "sym": sym,
                        "side": side,
                        "strategy": pos.get("strategy", "Auto"),
                        "entry": ep,
                        "exit": exit_price,
                        "investment": cfg["trade_investment"],
                        "pnl": round(raw_pnl, 4),
                        "entry_ts": pos["entry_ts"],
                        "exit_ts": int(time.time() * 1000),
                        "leverage": pos["leverage"],
                        "win": win,
                        "status": "CLOSED",
                    }
                    trade_history.insert(0, trade)
                    if len(trade_history) > 200:
                        trade_history.pop()
                    stats["total_pnl"]    = round(stats["total_pnl"] + raw_pnl, 4)
                    stats["today_pnl"]    = round(stats["today_pnl"] + raw_pnl, 4)
                    stats["total_trades"] += 1
                    if win:
                        stats["wins"] += 1
                        stats["best_trade"] = max(stats["best_trade"], raw_pnl)
                    else:
                        stats["losses"] += 1
                    stats["balance"] = await get_account_balance()
                    await broadcast("trade_closed", trade)
                    log.info(f"Trade closed: {sym} PnL={raw_pnl:.4f}")

                await broadcast("positions_update", {
                    "positions": list(open_positions.values()),
                    "stats": stats,
                })
        except Exception as e:
            log.error(f"Monitor error: {e}")
        await asyncio.sleep(3)

# ── Auto scalp engine ──────────────────────────────────────────────────────
SCAN_COINS = [
    "BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT","ADAUSDT","DOGEUSDT","AVAXUSDT",
    "DOTUSDT","LINKUSDT","UNIUSDT","LTCUSDT","ATOMUSDT","NEARUSDT","FTMUSDT","SANDUSDT",
    "MANAUSDT","AXSUSDT","AAVEUSDT","GMXUSDT","OPUSDT","ARBUSDT","SUIUSDT","INJUSDT",
    "APTUSDT","TIAUSDT","LDOUSDT","DYDXUSDT","IMXUSDT","RNDRUSDT","WIFUSDT","ENAUSDT",
    "JUPUSDT","ORDIUSDT","TAOUSDT","RENDERUSDT","STXUSDT","MINAUSDT","HBARUSDT","ICPUSDT",
]

scan_idx = 0

async def auto_scalp_loop():
    """The full automated scalp pipeline: Scan → Signal → Risk → Order → Monitor."""
    global scan_idx
    while True:
        try:
            if not cfg.get("bot_running") or not cfg.get("api_key"):
                await asyncio.sleep(1)
                continue

            interval = cfg.get("scan_interval_sec", 4)
            await asyncio.sleep(interval)

            # ── STAGE 1: Scan ──
            symbol = SCAN_COINS[scan_idx % len(SCAN_COINS)]
            scan_idx += 1
            await broadcast("pipeline", {"stage": "scanning", "coin": symbol, "count": scan_idx})

            # ── STAGE 2: Signal ──
            active = cfg.get("active_strategies", ["ema"])
            import random
            strat_id = random.choice(active)
            signal = await compute_signal(symbol, strat_id)
            score  = signal["score"]
            side   = signal["side"]
            await broadcast("pipeline", {
                "stage": "signal", "score": score, "side": side,
                "strategy": signal.get("strategy",""),
                "indicators": signal.get("indicators", {}),
            })

            # ── STAGE 3: Risk filter ──
            too_many = len(open_positions) >= cfg.get("max_open_positions", 5)
            low_sig  = score < cfg.get("signal_threshold", 65)
            already_open = symbol in open_positions
            risk_pass = not too_many and not low_sig and not already_open
            reason = "PASS" if risk_pass else ("MAX POSITIONS" if too_many else "ALREADY OPEN" if already_open else "LOW SIGNAL")
            await broadcast("pipeline", {"stage": "risk", "result": reason, "pass": risk_pass})

            if not risk_pass:
                continue

            # ── STAGE 4: Place order ──
            lev = cfg.get("leverage", 5)
            invest = cfg.get("trade_investment", 50)
            await broadcast("pipeline", {"stage": "order", "side": side, "symbol": symbol})

            try:
                order = await place_order(symbol, side, invest, lev)
                entry_price = order["entry_price"]
                qty = order["quantity"]
            except Exception as e:
                log.error(f"Order failed {symbol}: {e}")
                await broadcast("pipeline", {"stage": "order_failed", "error": str(e)})
                continue

            # ── STAGE 5: Place TP/SL ──
            tp_pct = cfg.get("take_profit_pct", 3.0)
            sl_pct = cfg.get("stop_loss_pct", 1.5)
            await place_tp_sl(symbol, side, qty, entry_price, tp_pct, sl_pct)

            # Track position locally
            pos_id = f"{symbol}-{int(time.time()*1000)}"
            open_positions[symbol] = {
                "id": pos_id, "sym": symbol, "side": side,
                "strategy": signal.get("strategy", "Auto"),
                "entry_price": entry_price, "current_price": entry_price,
                "quantity": qty, "leverage": lev,
                "investment": invest, "unrealised_pnl": 0.0,
                "entry_ts": int(time.time() * 1000),
                "tp_target": entry_price * (1 + tp_pct/100) if side == "LONG" else entry_price * (1 - tp_pct/100),
                "sl_target": entry_price * (1 - sl_pct/100) if side == "LONG" else entry_price * (1 + sl_pct/100),
                "signal": score,
            }
            await broadcast("pipeline", {
                "stage": "monitor", "symbol": symbol,
                "entry": entry_price, "qty": qty, "side": side,
            })
            await broadcast("positions_update", {"positions": list(open_positions.values()), "stats": stats})
            log.info(f"Opened {side} {symbol} @ {entry_price} qty={qty}")

        except Exception as e:
            log.error(f"Scalp loop error: {e}")
            await asyncio.sleep(2)

# ── Binance price WebSocket feed ───────────────────────────────────────────
async def price_ws_feed():
    """Stream top coin prices from Binance WebSocket and broadcast to dashboard."""
    streams = "/".join(f"{s.lower()}@miniTicker" for s in SCAN_COINS[:20])
    uri = f"{ws_base()}/stream?streams={streams}"
    while True:
        try:
            async with websockets.connect(uri, ping_interval=20) as ws:
                log.info("Binance price WebSocket connected")
                async for raw in ws:
                    msg = json.loads(raw)
                    tick = msg.get("data", msg)
                    sym = tick.get("s", "")
                    price = float(tick.get("c", 0))
                    if sym:
                        price_cache[sym] = price
                await broadcast("prices", price_cache)
        except Exception as e:
            log.warning(f"Price WS error: {e}, reconnecting in 5s")
            await asyncio.sleep(5)

# ── App lifespan ───────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(auto_scalp_loop())
    asyncio.create_task(monitor_positions())
    asyncio.create_task(price_ws_feed())
    yield

app = FastAPI(title="MakStanleyz Scalp Engine", lifespan=lifespan)

app.add_middleware(CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"])

# ── REST endpoints ─────────────────────────────────────────────────────────

class ConfigUpdate(BaseModel):
    api_key:            Optional[str]   = None
    api_secret:         Optional[str]   = None
    testnet:            Optional[bool]  = None
    leverage:           Optional[int]   = None
    trade_investment:   Optional[float] = None
    stop_loss_pct:      Optional[float] = None
    take_profit_pct:    Optional[float] = None
    scan_interval_sec:  Optional[int]   = None
    max_open_positions: Optional[int]   = None
    signal_threshold:   Optional[int]   = None
    bot_running:        Optional[bool]  = None
    active_strategies:  Optional[list]  = None

@app.get("/api/status")
async def get_status():
    return {
        "bot_running":    cfg["bot_running"],
        "testnet":        cfg["testnet"],
        "has_keys":       bool(cfg.get("api_key") and cfg.get("api_secret")),
        "leverage":       cfg["leverage"],
        "trade_invest":   cfg["trade_investment"],
        "sl_pct":         cfg["stop_loss_pct"],
        "tp_pct":         cfg["take_profit_pct"],
        "freq":           cfg["scan_interval_sec"],
        "max_pos":        cfg["max_open_positions"],
        "sig_thresh":     cfg["signal_threshold"],
        "active_strats":  cfg["active_strategies"],
        "open_positions": list(open_positions.values()),
        "stats":          stats,
        "trade_history":  trade_history[:50],
        "price_cache":    price_cache,
    }

@app.post("/api/config")
async def update_config(body: ConfigUpdate):
    updated = body.model_dump(exclude_none=True)
    cfg.update(updated)
    # Persist to config.json
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)
    log.info(f"Config updated: {list(updated.keys())}")
    return {"ok": True, "config": cfg}

@app.post("/api/bot/start")
async def bot_start():
    if not cfg.get("api_key") or not cfg.get("api_secret"):
        raise HTTPException(400, "API keys not configured")
    cfg["bot_running"] = True
    stats["balance"] = await get_account_balance()
    await broadcast("bot_status", {"running": True})
    return {"ok": True, "message": "Bot started"}

@app.post("/api/bot/stop")
async def bot_stop():
    cfg["bot_running"] = False
    await broadcast("bot_status", {"running": False})
    return {"ok": True, "message": "Bot stopped"}

@app.get("/api/balance")
async def get_balance():
    bal = await get_account_balance()
    stats["balance"] = bal
    return {"balance": bal, "currency": "USDT"}

@app.get("/api/positions")
async def get_positions():
    return {"positions": list(open_positions.values()), "count": len(open_positions)}

@app.get("/api/trades")
async def get_trades(limit: int = 100):
    return {"trades": trade_history[:limit], "total": len(trade_history)}

@app.post("/api/trade/manual")
async def manual_trade(symbol: str, side: str):
    """Place a manual market order."""
    if not cfg.get("api_key"):
        raise HTTPException(400, "API keys not configured")
    side = side.upper()
    if side not in ("LONG", "SHORT"):
        raise HTTPException(400, "side must be LONG or SHORT")
    try:
        order = await place_order(symbol.upper(), side, cfg["trade_investment"], cfg["leverage"])
        ep = order["entry_price"]
        await place_tp_sl(symbol, side, order["quantity"], ep,
                          cfg["take_profit_pct"], cfg["stop_loss_pct"])
        open_positions[symbol] = {
            "id": f"M-{int(time.time()*1000)}", "sym": symbol, "side": side,
            "strategy": "Manual", "entry_price": ep, "current_price": ep,
            "quantity": order["quantity"], "leverage": cfg["leverage"],
            "investment": cfg["trade_investment"], "unrealised_pnl": 0.0,
            "entry_ts": int(time.time() * 1000),
        }
        return {"ok": True, "order": order}
    except Exception as e:
        raise HTTPException(500, str(e))

@app.post("/api/trade/close/{symbol}")
async def close_position(symbol: str):
    """Close a specific open position."""
    symbol = symbol.upper()
    if symbol not in open_positions:
        raise HTTPException(404, "Position not found")
    pos = open_positions[symbol]
    side = pos["side"]
    qty = pos["quantity"]
    try:
        await cancel_all_orders(symbol)
        await binance_post("/fapi/v1/order", {
            "symbol": symbol,
            "side": "SELL" if side == "LONG" else "BUY",
            "type": "MARKET",
            "quantity": str(qty),
            "reduceOnly": "true",
            "positionSide": "BOTH",
        })
        return {"ok": True, "message": f"Closed {symbol}"}
    except Exception as e:
        raise HTTPException(500, str(e))

@app.post("/api/close_all")
async def close_all_positions():
    """Emergency close all open positions."""
    results = []
    for sym in list(open_positions.keys()):
        try:
            pos = open_positions[sym]
            await cancel_all_orders(sym)
            await binance_post("/fapi/v1/order", {
                "symbol": sym,
                "side": "SELL" if pos["side"] == "LONG" else "BUY",
                "type": "MARKET",
                "quantity": str(pos["quantity"]),
                "reduceOnly": "true",
                "positionSide": "BOTH",
            })
            results.append({"sym": sym, "ok": True})
        except Exception as e:
            results.append({"sym": sym, "ok": False, "error": str(e)})
    return {"results": results}

@app.get("/api/signal/{symbol}")
async def get_signal(symbol: str, strategy: str = "ema"):
    sig = await compute_signal(symbol.upper(), strategy)
    return sig

@app.get("/api/market/prices")
async def get_prices():
    return {"prices": price_cache}

@app.get("/api/market/klines/{symbol}")
async def get_klines(symbol: str, interval: str = "1m", limit: int = 80):
    data = await binance_get("/fapi/v1/klines", {
        "symbol": symbol.upper(), "interval": interval, "limit": limit
    })
    return {"klines": [{"time": k[0], "open": float(k[1]), "high": float(k[2]),
                         "low": float(k[3]), "close": float(k[4]), "volume": float(k[5])}
                        for k in data]}

# ── Dashboard WebSocket ────────────────────────────────────────────────────
@app.websocket("/ws")
async def dashboard_ws(ws: WebSocket):
    await ws.accept()
    ws_clients.append(ws)
    # Send initial state
    await ws.send_text(json.dumps({"event": "init", "data": {
        "bot_running":    cfg["bot_running"],
        "testnet":        cfg["testnet"],
        "positions":      list(open_positions.values()),
        "stats":          stats,
        "trades":         trade_history[:50],
        "prices":         price_cache,
        "active_strats":  cfg["active_strategies"],
    }}))
    try:
        while True:
            await ws.receive_text()  # keep alive
    except WebSocketDisconnect:
        ws_clients.remove(ws)

@app.get("/")
async def root():
    return {
        "name": "MakStanleyz Scalp Engine",
        "version": "4.0",
        "testnet": cfg["testnet"],
        "docs": "/docs",
    }
