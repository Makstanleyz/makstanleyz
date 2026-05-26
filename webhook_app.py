"""
webhook_app.py — MakStanleyz Railway Webhook Server
TradingView 1m alert → POST /webhook → execute on Binance Futures
"""
from flask import Flask, request, jsonify, render_template_string
import asyncio, os, time, logging, threading
import ccxt.async_support as ccxt
from core.order_executor import OrderExecutor
from core.paper_executor import PaperExecutor
from core.position_manager import PositionManager
from core.exit_engine import ExitEngine
from core.risk_manager import RiskManager
from core.market_scanner import MarketScanner, MarketContext
from core.scanner import MakStanleyzScanner
from utils import telegram_bot as tg
import config

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("webhook")

app           = Flask(__name__)
START_TIME    = time.time()
risk_mgr      = RiskManager()
_positions: list = []
_mkt_context  = MarketContext()
_mkt_last_scan = 0.0

_scan_results: list = []
_scan_last_run: float = 0.0
_scan_running:  bool  = False

_loop   = asyncio.new_event_loop()
_thread = threading.Thread(target=_loop.run_forever, daemon=True)
_thread.start()

if config.PAPER_MODE:
    exchange    = None
    executor    = PaperExecutor()
    pos_manager = None
    exit_eng    = None
    mkt_scanner = None
    logger.info("PAPER MODE active — no exchange connection")
else:
    exchange = ccxt.binance({
        "apiKey":  config.API_KEY,
        "secret":  config.API_SECRET,
        "options": {"defaultType": "future"},
        "sandbox": config.TESTNET,
    })
    executor    = OrderExecutor(exchange)
    pos_manager = PositionManager(exchange)
    exit_eng    = ExitEngine(exchange)
    mkt_scanner = MarketScanner(exchange)

_bot_scanner = MakStanleyzScanner()


def run(coro):
    return asyncio.run_coroutine_threadsafe(coro, _loop).result(timeout=30)


def check_secret(data):
    return str(data.get("secret", "")) == str(os.getenv("WEBHOOK_SECRET", ""))


def _sig_to_dict(s) -> dict:
    return {
        "symbol":     s.symbol,
        "direction":  s.direction,
        "score":      s.score,
        "price":      s.price,
        "rsi":        s.rsi,
        "vol_ratio":  s.vol_ratio,
        "change_24h": s.change_24h,
        "is_gem":     s.is_gem,
        "fund_rate":  s.fund_rate,
    }


def _run_scan_bg():
    global _scan_results, _scan_last_run, _scan_running
    if _scan_running:
        return
    _scan_running = True
    try:
        future  = asyncio.run_coroutine_threadsafe(_bot_scanner.scan_all(), _loop)
        signals = future.result(timeout=120)
        _scan_results  = signals
        _scan_last_run = time.time()
        logger.info("Scan complete: %d signals", len(signals))
    except Exception as e:
        logger.error("Background scan error: %s", e)
    finally:
        _scan_running = False


def _scan_scheduler():
    while True:
        time.sleep(config.SCAN_INTERVAL_SEC)
        _run_scan_bg()


threading.Thread(target=_scan_scheduler, daemon=True, name="scan-scheduler").start()
threading.Thread(target=_run_scan_bg,    daemon=True, name="scan-initial").start()


# ─────────────────────────────────────────────────────────────────────────────
# Dashboard HTML
# ─────────────────────────────────────────────────────────────────────────────
DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>MakStanleyz — Live Dashboard</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{--bg0:#29ABE2;--bg1:#081929;--bg2:#0C2033;--bd:rgba(255,255,255,0.14);--b:#29ABE2;--g:#00E5A0;--r:#FF4757;--y:#FFD700;--txt:#ffffff;--txt2:rgba(255,255,255,0.6);--txt3:rgba(255,255,255,0.38)}
body{background:var(--bg0);color:var(--txt);font-family:'Courier New',Consolas,monospace;font-size:13px;min-height:100vh}
/* scrollbar */
::-webkit-scrollbar{width:5px;height:5px}
::-webkit-scrollbar-track{background:rgba(0,0,0,0.12)}
::-webkit-scrollbar-thumb{background:rgba(255,255,255,0.35);border-radius:3px}
/* header */
.hdr{background:linear-gradient(90deg,#060E1A,#0D2035);border-bottom:1px solid rgba(255,255,255,0.1);padding:14px 24px;display:flex;align-items:center;gap:16px;flex-wrap:wrap;box-shadow:0 3px 20px rgba(0,0,0,0.55)}
.hdr h1{color:#fff;font-size:1.18rem;flex:1;letter-spacing:1px;text-shadow:0 0 20px rgba(41,171,226,0.6)}
.hdr h1 span{background:linear-gradient(90deg,#29ABE2,#00E5A0);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.hdr-meta{display:flex;gap:10px;align-items:center;font-size:.75rem;flex-wrap:wrap}
/* tabs */
.tabs{display:flex;border-bottom:2px solid rgba(255,255,255,0.1);padding:0 20px;overflow-x:auto;background:#081929}
.tab{background:none;border:none;color:rgba(255,255,255,0.4);padding:12px 20px;cursor:pointer;font-family:inherit;font-size:.8rem;letter-spacing:.6px;border-bottom:3px solid transparent;margin-bottom:-2px;white-space:nowrap;transition:all .15s;font-weight:700}
.tab:hover{color:rgba(255,255,255,0.7)}
.tab.active{color:#ffffff;border-bottom-color:#ffffff}
/* panels */
.panel{display:none;padding:20px 22px}
.panel.active{display:block}
.row{display:grid;grid-template-columns:repeat(auto-fit,minmax(270px,1fr));gap:16px;margin-bottom:18px}
/* cards */
.card{background:var(--bg2);border:1px solid rgba(255,255,255,0.1);border-radius:10px;padding:16px;box-shadow:0 4px 24px rgba(0,0,0,0.45)}
.card-title{color:rgba(41,171,226,0.8);font-size:.7rem;text-transform:uppercase;letter-spacing:1.4px;margin-bottom:12px;padding-bottom:8px;border-bottom:1px solid var(--bd);font-weight:700}
/* table */
table{width:100%;border-collapse:collapse}
th,td{padding:8px 10px;text-align:left;border-bottom:1px solid rgba(41,171,226,0.09);white-space:nowrap}
th{color:rgba(41,171,226,0.6);font-weight:700;font-size:.72rem;text-transform:uppercase;letter-spacing:.8px}
tr:last-child td{border-bottom:none}
tr:hover td{background:rgba(41,171,226,0.04)}
tfoot td{border-top:1px solid var(--bd);border-bottom:none}
/* colour tokens */
.g{color:var(--g)}.r{color:var(--r)}.b{color:var(--b)}.y{color:var(--y)}.gr{color:var(--txt3)}
/* badges */
.badge{display:inline-block;padding:3px 9px;border-radius:5px;font-size:.7rem;font-weight:800;letter-spacing:.4px}
.bg{background:rgba(0,229,160,0.18);color:#00E5A0;border:1px solid rgba(0,229,160,0.4)}
.br{background:rgba(255,71,87,0.18);color:#FF4757;border:1px solid rgba(255,71,87,0.4)}
.bb{background:rgba(41,171,226,0.18);color:#29ABE2;border:1px solid rgba(41,171,226,0.4)}
.bgr{background:rgba(255,255,255,0.06);color:var(--txt3);border:1px solid rgba(255,255,255,0.12)}
.by{background:rgba(255,215,0,0.15);color:#FFD700;border:1px solid rgba(255,215,0,0.35)}
/* scan bar */
.scan-bar{display:flex;gap:12px;align-items:center;margin-bottom:16px;flex-wrap:wrap}
.btn{background:#ffffff;color:#091929;border:none;padding:9px 20px;border-radius:7px;cursor:pointer;font-family:inherit;font-size:.82rem;font-weight:800;letter-spacing:.5px;box-shadow:0 2px 12px rgba(0,0,0,0.28);transition:all .15s}
.btn:hover{filter:brightness(0.94);transform:translateY(-1px)}
.btn:active{transform:translateY(0)}
.btn:disabled{opacity:.38;cursor:not-allowed;box-shadow:none;transform:none}
.btn-danger{background:var(--r);color:#fff;box-shadow:0 2px 10px rgba(255,71,87,0.45)}
.scan-info{color:var(--txt2);font-size:.76rem}
.spin{display:inline-block;animation:spin .8s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
/* countdown */
#cd{color:#ffffff;font-size:.76rem;background:rgba(255,255,255,0.15);padding:3px 10px;border-radius:5px;border:1px solid rgba(255,255,255,0.25)}
/* empty state */
.empty{padding:28px;text-align:center;color:var(--txt3);font-size:.9rem}
.empty-icon{font-size:2rem;margin-bottom:8px}
/* position card */
.pos-card{background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.1);border-radius:8px;padding:14px;margin-bottom:12px}
.pos-card table th{color:var(--txt3)}
</style>
</head>
<body>

<div class="hdr">
  <h1>🌊 <span>MakStanleyz</span> — Live Scalping Dashboard</h1>
  <div class="hdr-meta">
    <span id="hdr-mode"></span>
    <span id="hdr-trade"></span>
    <span id="hdr-pos"></span>
    <span id="cd">↻ 60s</span>
  </div>
</div>

<div class="tabs">
  <button class="tab active" onclick="openTab('scanner',this)">📡 Live Scanner</button>
  <button class="tab" onclick="openTab('longs',this)">📈 Longs</button>
  <button class="tab" onclick="openTab('shorts',this)">📉 Shorts</button>
  <button class="tab" onclick="openTab('dca',this)">💼 Positions</button>
</div>

<div id="panel-scanner" class="panel active">
  <div class="row">
    <div class="card">
      <div class="card-title">📊 Market Context</div>
      <table><tbody id="mkt-tbl"><tr><td class="gr">Loading…</td></tr></tbody></table>
    </div>
    <div class="card">
      <div class="card-title">🤖 Bot Status</div>
      <table><tbody id="bot-tbl"><tr><td class="gr">Loading…</td></tr></tbody></table>
    </div>
    <div class="card">
      <div class="card-title">🛡️ Risk Monitor</div>
      <table><tbody id="risk-tbl"><tr><td class="gr">Loading…</td></tr></tbody></table>
    </div>
  </div>
  <div class="card">
    <div class="card-title">⚡ Signals — 1m Entry · 5m Trend Filter</div>
    <div class="scan-bar">
      <button id="scan-btn" class="btn" onclick="triggerScan()">▶ Scan Now</button>
      <span id="scan-status" class="scan-info">Not yet scanned</span>
    </div>
    <div style="overflow-x:auto">
      <table>
        <thead><tr><th>Symbol</th><th>Direction</th><th>Score</th><th>RSI</th><th>Vol×</th><th>24h %</th><th>Fund %</th><th>Gem</th></tr></thead>
        <tbody id="scanner-tbody"><tr><td colspan="8"><div class="empty"><div class="empty-icon">📡</div>Click Scan Now to detect signals</div></td></tr></tbody>
      </table>
    </div>
  </div>
</div>

<div id="panel-longs" class="panel">
  <div class="card">
    <div class="card-title">📈 Long Signals</div>
    <div style="overflow-x:auto">
      <table>
        <thead><tr><th>Symbol</th><th>Score</th><th>RSI</th><th>Vol×</th><th>24h %</th><th>Fund %</th><th>Gem</th></tr></thead>
        <tbody id="longs-tbody"><tr><td colspan="7"><div class="empty"><div class="empty-icon">📈</div>No long signals yet</div></td></tr></tbody>
      </table>
    </div>
  </div>
</div>

<div id="panel-shorts" class="panel">
  <div class="card">
    <div class="card-title">📉 Short Signals</div>
    <div style="overflow-x:auto">
      <table>
        <thead><tr><th>Symbol</th><th>Score</th><th>RSI</th><th>Vol×</th><th>24h %</th><th>Fund %</th><th>Gem</th></tr></thead>
        <tbody id="shorts-tbody"><tr><td colspan="7"><div class="empty"><div class="empty-icon">📉</div>No short signals yet</div></td></tr></tbody>
      </table>
    </div>
  </div>
</div>

<div id="panel-dca" class="panel">
  <div class="card">
    <div class="card-title">💼 Open Positions</div>
    <div id="pos-content"><div class="empty"><div class="empty-icon">💤</div>No open positions</div></div>
  </div>
</div>

<script>
let _countdown=60;
function openTab(n,b){document.querySelectorAll('.panel').forEach(p=>p.classList.remove('active'));document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));document.getElementById('panel-'+n).classList.add('active');b.classList.add('active');}
function bdg(t,c){return`<span class="badge ${c}">${t}</span>`;}
function sigRow(s,d){const sym=s.symbol.replace('/USDT:USDT','');const dir=s.direction==='long'?bdg('LONG','bg'):bdg('SHORT','br');const gem=s.is_gem?'<span class="y">★</span>':'<span class="gr">—</span>';const cc=s.change_24h>=0?'g':'r';const fp=(s.fund_rate*100).toFixed(4);const fc=s.fund_rate<-0.00001?'g':s.fund_rate>0.00001?'r':'gr';const rc=s.rsi<30?'g':s.rsi>70?'r':'';if(d)return`<tr><td class="b" style="font-weight:700">${sym}</td><td>${dir}</td><td style="font-weight:700">${s.score}</td><td class="${rc}">${s.rsi}</td><td>${s.vol_ratio}×</td><td class="${cc}">${s.change_24h}%</td><td class="${fc}">${fp}%</td><td>${gem}</td></tr>`;return`<tr><td class="b" style="font-weight:700">${sym}</td><td style="font-weight:700">${s.score}</td><td class="${rc}">${s.rsi}</td><td>${s.vol_ratio}×</td><td class="${cc}">${s.change_24h}%</td><td class="${fc}">${fp}%</td><td>${gem}</td></tr>`;}
function renderScan(data){const sigs=data.signals||[];const age=data.age_secs!=null?`${data.age_secs}s ago`:'never';document.getElementById('scan-status').innerHTML=data.running?'<span class="spin">⟳</span>&nbsp;Scanning markets…':sigs.length?`<span class="g">✓</span> ${sigs.length} signals found · last ${age}`:`Last scan: ${age} — no signals`;document.getElementById('scan-btn').disabled=data.running;document.getElementById('scanner-tbody').innerHTML=sigs.length?sigs.map(s=>sigRow(s,true)).join(''):'<tr><td colspan="8"><div class="empty"><div class="empty-icon">🔍</div>No signals above min score (80)</div></td></tr>';const longs=sigs.filter(s=>s.direction==='long');const shorts=sigs.filter(s=>s.direction==='short');document.getElementById('longs-tbody').innerHTML=longs.length?longs.map(s=>sigRow(s,false)).join(''):'<tr><td colspan="7"><div class="empty"><div class="empty-icon">📈</div>No long signals</div></td></tr>';document.getElementById('shorts-tbody').innerHTML=shorts.length?shorts.map(s=>sigRow(s,false)).join(''):'<tr><td colspan="7"><div class="empty"><div class="empty-icon">📉</div>No short signals</div></td></tr>';}
function renderContext(m){const gb=(m.allow_long||m.allow_short)?bdg('OPEN','bg'):bdg('BLOCKED','br');document.getElementById('mkt-tbl').innerHTML=`<tr><th>BTC Trend</th><td class="b">${m.btc_trend.toUpperCase()}</td></tr><tr><th>ADX</th><td class="${m.btc_adx>25?'r':'g'}">${m.btc_adx.toFixed(1)}</td></tr><tr><th>Breadth</th><td>${m.breadth_pct.toFixed(0)}% above EMA50</td></tr><tr><th>Gate</th><td>${gb}</td></tr><tr><th>Leverage Cap</th><td class="y">${m.leverage_cap}×</td></tr>`;}
function renderStatus(data){const r=data.risk;document.getElementById('risk-tbl').innerHTML=`<tr><th>Daily P&L</th><td class="${r.daily_pnl>=0?'g':'r'}" style="font-weight:700">$${r.daily_pnl.toFixed(2)}</td></tr><tr><th>Daily Loss</th><td class="${r.daily_loss>0?'r':'gr'}">$${r.daily_loss.toFixed(2)}</td></tr><tr><th>Limit</th><td>$${r.daily_limit.toFixed(2)}</td></tr><tr><th>Remaining</th><td class="${r.remaining_risk>5?'g':'r'}" style="font-weight:700">$${r.remaining_risk.toFixed(2)}</td></tr><tr><th>Trades Today</th><td>${r.trades_today}</td></tr>`;const positions=data.positions||[];document.getElementById('pos-content').innerHTML=positions.length?positions.map(p=>`<div class="pos-card"><table><tr><th>Symbol</th><td class="b" style="font-weight:700">${p.symbol}</td></tr><tr><th>Direction</th><td>${p.direction==='long'?bdg('LONG','bg'):bdg('SHORT','br')}</td></tr><tr><th>Leverage</th><td class="y">${p.leverage}×</td></tr><tr><th>Avg Entry</th><td>$${p.avg_entry.toFixed(4)}</td></tr><tr><th>Trail Stop</th><td class="r">$${p.trail_stop.toFixed(4)}</td></tr></table></div>`).join(''):'<div class="empty"><div class="empty-icon">💤</div>No open positions</div>';}
function renderHealth(data){const mc=data.mode==='live'?'br':'bb';document.getElementById('hdr-mode').innerHTML=bdg(data.mode.toUpperCase(),mc);document.getElementById('hdr-trade').innerHTML=data.halted?bdg('HALTED','br'):bdg('ACTIVE','bg');document.getElementById('hdr-pos').textContent=data.position_count?`🔴 ${data.position_count} open`:'';const up=data.uptime_s;document.getElementById('bot-tbl').innerHTML=`<tr><th>Mode</th><td>${bdg(data.mode.toUpperCase(),mc)}</td></tr><tr><th>Trading</th><td>${data.halted?bdg('HALTED','br'):bdg('ACTIVE','bg')}</td></tr><tr><th>Positions</th><td class="${data.position_count?'b':'gr'}">${data.position_count}/${data.max_positions||1}</td></tr><tr><th>Daily P&L</th><td class="${data.daily_pnl>=0?'g':'r'}" style="font-weight:700">$${data.daily_pnl.toFixed(2)}</td></tr><tr><th>Uptime</th><td class="gr">${Math.floor(up/3600)}h ${Math.floor(up%3600/60)}m</td></tr>`;}
async function loadAll(){try{const[hR,cR,sR,scR]=await Promise.all([fetch('/health'),fetch('/context'),fetch('/status'),fetch('/scan')]);renderHealth(await hR.json());renderContext(await cR.json());renderStatus(await sR.json());renderScan(await scR.json());}catch(e){console.error(e);}}
async function triggerScan(){document.getElementById('scan-btn').disabled=true;document.getElementById('scan-status').innerHTML='<span class="spin">⟳</span>&nbsp;Scan triggered…';await fetch('/scan',{method:'POST'});setTimeout(loadAll,3000);}
setInterval(()=>{_countdown--;document.getElementById('cd').textContent=`↻ ${_countdown}s`;if(_countdown<=0){_countdown=60;loadAll();}},1000);
loadAll();
</script>
</body>
</html>"""


# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/")
def dashboard():
    return render_template_string(DASHBOARD_HTML)


@app.route("/scan", methods=["GET", "POST"])
def scan_endpoint():
    if request.method == "POST":
        if not _scan_running:
            threading.Thread(target=_run_scan_bg, daemon=True, name="scan-manual").start()
        return jsonify({"status": "scan_started", "running": _scan_running}), 200
    return jsonify({
        "last_run": _scan_last_run,
        "age_secs": round(time.time() - _scan_last_run) if _scan_last_run else None,
        "running":  _scan_running,
        "count":    len(_scan_results),
        "signals":  [_sig_to_dict(s) for s in _scan_results],
    }), 200


@app.route("/context")
def context():
    return jsonify({
        "btc_trend":    _mkt_context.btc_trend,
        "btc_regime":   _mkt_context.btc_regime,
        "btc_adx":      round(_mkt_context.btc_adx, 1),
        "breadth_pct":  round(_mkt_context.breadth_pct, 1),
        "allow_long":   _mkt_context.allow_long,
        "allow_short":  _mkt_context.allow_short,
        "leverage_cap": _mkt_context.leverage_cap,
        "reason":       _mkt_context.reason,
    }), 200


@app.route("/health")
def health():
    open_pos = [p for p in _positions if p.is_open]
    return jsonify({
        "status":         "ok",
        "uptime_s":       round(time.time() - START_TIME),
        "mode":           "paper" if config.PAPER_MODE else ("testnet" if config.TESTNET else "live"),
        "position":       ", ".join(p.symbol for p in open_pos) if open_pos else None,
        "position_count": len(open_pos),
        "max_positions":  config.MAX_POSITIONS,
        "daily_pnl":      risk_mgr.status()["daily_pnl"],
        "halted":         risk_mgr.trading_halted,
    }), 200


def _pos_to_dict(p) -> dict:
    return {
        "symbol":       p.symbol,
        "direction":    p.direction,
        "leverage":     p.leverage,
        "avg_entry":    round(p.avg_entry, 4),
        "contracts":    round(p.total_contracts, 4),
        "total_margin": round(p.total_margin, 2),
        "dca_margin":   round(p.dca_margin, 2),
        "cushion_used": p.cushion_used,
        "recovery_tp":  p.recovery_tp,
        "trail_stop":   round(p.trail_stop, 4),
        "dca_levels": [
            {
                "entry_num":    l.entry_num,
                "target_price": round(l.target_price, 4),
                "margin":       round(l.margin, 2),
                "contracts":    round(l.contracts, 4),
                "filled":       l.filled,
                "fill_price":   round(l.fill_price, 4) if l.fill_price else None,
            }
            for l in p.dca_levels
        ],
    }


@app.route("/status")
def status():
    open_pos = [_pos_to_dict(p) for p in _positions if p.is_open]
    return jsonify({
        "risk":      risk_mgr.status(),
        "positions": open_pos,
        "position":  open_pos[0] if open_pos else None,
    }), 200


@app.route("/reset", methods=["POST"])
def reset():
    if not config.PAPER_MODE:
        return jsonify({"error": "only allowed in paper mode"}), 403
    data = request.get_json(force=True, silent=True) or {}
    if not check_secret(data):
        return jsonify({"error": "unauthorised"}), 401
    _positions.clear()
    risk_mgr.__init__()
    logger.info("Paper state reset via /reset")
    return jsonify({"status": "reset"}), 200


def _find_position(symbol: str):
    base = symbol.split(".")[0]
    if base.endswith("USDT"):
        base = base[:-4]
    full = base + "/USDT:USDT"
    for p in _positions:
        if p.is_open and p.symbol in (symbol, full):
            return p
    return None


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(force=True, silent=True)
    if not data:
        return jsonify({"error": "invalid JSON"}), 400
    if not check_secret(data):
        logger.warning("Unauthorised webhook from %s", request.remote_addr)
        return jsonify({"error": "unauthorised"}), 401

    action    = data.get("action", "").lower()
    symbol    = data.get("symbol", "")
    score     = int(data.get("score", 0))
    leverage  = int(data.get("leverage", 5))
    direction = data.get("direction", action)
    price     = float(data.get("price", 0))

    logger.info("Webhook: action=%s symbol=%s score=%d price=%.4f",
                action, symbol, score, price)

    if action in ("long", "short"):
        open_count = sum(1 for p in _positions if p.is_open)
        if open_count >= config.MAX_POSITIONS:
            return jsonify({"status": "skip", "reason": "max positions reached"}), 200
        if _find_position(symbol):
            return jsonify({"status": "skip", "reason": "symbol already open"}), 200

        global _mkt_context, _mkt_last_scan
        if (not config.PAPER_MODE and mkt_scanner and
                time.time() - _mkt_last_scan > config.MARKET_CONTEXT_INTERVAL_SEC):
            _mkt_context  = run(mkt_scanner.scan())
            _mkt_last_scan = time.time()

        if not _mkt_context.allow_long and not _mkt_context.allow_short:
            msg = f"Blocked — ADX={_mkt_context.btc_adx:.0f} (extreme trend)"
            run(tg.alert_error(f"Signal blocked: {msg}"))
            return jsonify({"status": "blocked", "reason": msg}), 200

        leverage = min(leverage, _mkt_context.leverage_cap)
        allowed, reason = risk_mgr.can_trade(score)
        if not allowed:
            run(tg.alert_error(f"Signal blocked: {reason}"))
            return jsonify({"status": "blocked", "reason": reason}), 200

        pos = run(_handle_entry(data, symbol, direction, leverage, score, price))
        if pos:
            _positions.append(pos)
        return jsonify({"status": "opened" if pos else "failed"}), 200

    elif action == "exit":
        pos = _find_position(symbol)
        if not pos:
            return jsonify({"status": "skip", "reason": "no matching position"}), 200
        run(_handle_exit(pos, price, data))
        return jsonify({"status": "closed"}), 200

    elif action == "inject":
        pos = _find_position(symbol)
        if pos:
            run(tg.alert_inject_urgent(pos, float(data.get("liq_dist", 5)),
                                       config.INJECT_TRANCHE))
        return jsonify({"status": "inject_alert_sent"}), 200

    elif action == "caution":
        pos = _find_position(symbol)
        if pos:
            run(tg.alert_caution(pos, int(data.get("flips", 2)), data))
        return jsonify({"status": "caution_sent"}), 200

    return jsonify({"error": "unknown action"}), 400


async def _handle_entry(data, symbol, direction, leverage, score, price):
    from core.signal_engine import ValidatedSignal
    from core.scanner import Signal
    base = symbol.split(".")[0]
    if base.endswith("USDT"):
        base = base[:-4]
    full = base + "/USDT:USDT"
    sig = Signal(symbol=full, direction=direction, score=score, price=price,
                 rsi=float(data.get("rsi", 50)), vol_ratio=float(data.get("vol_ratio", 1)),
                 change_24h=float(data.get("change_24h", 0)),
                 is_gem=(score >= 80), is_gainer=False, is_loser=False)

    # htf_aligned now reflects 5m state from Pine Script payload
    htf_raw = data.get("htf_aligned")
    btc_raw = data.get("btc_confirms")
    htf_aligned  = (True if htf_raw == "true"  or htf_raw is True
                    else False if htf_raw == "false" or htf_raw is False
                    else None)
    btc_confirms = (True if btc_raw == "true"  or btc_raw is True
                    else False if btc_raw == "false" or btc_raw is False
                    else None)

    val = ValidatedSignal(signal=sig, leverage=leverage,
                          htf_aligned=htf_aligned, btc_confirms=btc_confirms,
                          fund_rate=float(data.get("fund_rate", 0)),
                          score_mod=score, action="enter", reason="tv_webhook")
    await tg.alert_signal_found(sig, val)
    pos = await executor.open_position(val)
    if pos:
        risk_mgr.on_position_opened()
        await tg.alert_e1_filled(pos)
    else:
        await tg.alert_error(f"Failed to open {full}")
    return pos


async def _handle_exit(position, close_price, data: dict):
    exit_reason = data.get("exit_reason", "TRIPLE_CONFIRM")
    order = await executor.close_position(position, exit_reason)
    if order:
        fill = float(order.get("average") or close_price)
        notional = position.dca_margin * position.leverage
        if position.direction == "long":
            pnl = (fill - position.avg_entry) / position.avg_entry * notional
        else:
            pnl = (position.avg_entry - fill) / position.avg_entry * notional
        risk_mgr.on_position_closed(pnl)
        exit_eval = {"exit_reason": exit_reason, "should_exit": True,
                     "rsi_holds": data.get("rsi_holds", False),
                     "ema_holds": data.get("ema_holds", False),
                     "vwap_holds": data.get("vwap_holds", False),
                     "rsi": float(data.get("rsi", 50))}
        await tg.alert_exit_fired(position, exit_eval, pnl, fill)
        if config.PAPER_MODE:
            from core.paper_executor import log_paper_trade
            log_paper_trade(position, fill, pnl, exit_reason)
        _positions.remove(position)


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    logger.info("MakStanleyz webhook server starting | port=%d | paper=%s testnet=%s",
                port, config.PAPER_MODE, config.TESTNET)
    app.run(host="0.0.0.0", port=port, debug=False)
