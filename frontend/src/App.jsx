import { useState, useEffect, useRef, useCallback } from "react";

/* ══════════════════════════════════════════════════════════
   MakStanleyz — Live Dashboard (wired to FastAPI backend)
   ══════════════════════════════════════════════════════════ */

const BOT_NAME     = "MakStanleyz";
const BACKEND_HTTP = import.meta.env.VITE_BACKEND_URL || "http://localhost:8000";
const BACKEND_WS   = BACKEND_HTTP.replace("http", "ws") + "/ws";
const WIN_RATE_TGT = 85;

const STRATEGIES = [
  { id:"ema",  name:"Scalp EMA Cross",     color:"#00b4ff" },
  { id:"rsi",  name:"RSI Divergence",      color:"#a78bfa" },
  { id:"bb",   name:"Bollinger Squeeze",   color:"#f0c040" },
  { id:"vwap", name:"VWAP Reversion",      color:"#00d4a0" },
  { id:"obf",  name:"Order Flow Imbalance",color:"#ff9940" },
  { id:"brk",  name:"Breakout Hunter",     color:"#ff5060" },
  { id:"mom",  name:"Momentum Ride",       color:"#22d3ee" },
];

const fmtP = p => {
  if(!p || isNaN(p)) return "—";
  return p>=10000?Number(p).toFixed(0):p>=1000?Number(p).toFixed(1):p>=10?Number(p).toFixed(2):p>=1?Number(p).toFixed(3):p>=0.01?Number(p).toFixed(5):Number(p).toFixed(7);
};
const fmtDT = ts => {
  const d = new Date(ts);
  return {
    date: d.toLocaleDateString("en-GB",{day:"2-digit",month:"short",year:"2-digit"}),
    time: d.toLocaleTimeString("en-GB",{hour:"2-digit",minute:"2-digit",second:"2-digit"}),
  };
};

/* ── API helpers ────────────────────────────────────────────────────────── */
const api = {
  get:  (path)      => fetch(`${BACKEND_HTTP}${path}`).then(r=>r.json()),
  post: (path,body) => fetch(`${BACKEND_HTTP}${path}`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body||{})}).then(r=>r.json()),
  postQ:(path,params)=>{
    const url=new URL(`${BACKEND_HTTP}${path}`);
    Object.entries(params||{}).forEach(([k,v])=>url.searchParams.set(k,v));
    return fetch(url,{method:"POST"}).then(r=>r.json());
  },
};

/* ── Sub-components ────────────────────────────────────────────────────── */
const Dot = ({on,size=7})=>(
  <div style={{width:size,height:size,borderRadius:"50%",flexShrink:0,
    background:on?"#00d4a0":"#ff5060",
    boxShadow:on?"0 0 8px #00d4a0":"0 0 6px #ff5060",
    animation:on?"pulse 1.6s infinite":"none"}}/>
);

const WinGauge = ({rate})=>{
  const pct=parseFloat(rate)||0,r=26,cx=32,cy=32,circ=2*Math.PI*r;
  const col=pct>=80?"#00d4a0":pct>=60?"#f0c040":"#ff5060";
  return <svg width="64" height="64" style={{display:"block",flexShrink:0}}>
    <circle cx={cx} cy={cy} r={r} fill="none" stroke="rgba(255,255,255,0.06)" strokeWidth="4"/>
    <circle cx={cx} cy={cy} r={r} fill="none" stroke={col} strokeWidth="4"
      strokeDasharray={`${(pct/100)*circ} ${circ}`} strokeDashoffset={circ/4}
      strokeLinecap="round" style={{transition:"stroke-dasharray 0.6s ease"}}/>
    <text x={cx} y={cy-3} textAnchor="middle" fontSize="10" fontWeight="700" fill="#dde4f0">{pct.toFixed(1)}</text>
    <text x={cx} y={cy+9} textAnchor="middle" fontSize="6.5" fill="rgba(255,255,255,0.35)">WIN%</text>
  </svg>;
};

const Spark = ({data,color="#00b4ff",h=42})=>{
  if(!data?.length) return null;
  const W=200,mn=Math.min(...data),mx=Math.max(...data),rng=mx-mn||1;
  const pts=data.map((v,i)=>`${(i/(data.length-1))*W},${h-((v-mn)/rng)*(h-4)-2}`).join(" ");
  return <svg width="100%" viewBox={`0 0 ${W} ${h}`} style={{display:"block"}}>
    <polyline points={pts} fill="none" stroke={color} strokeWidth="1.5" strokeLinejoin="round"/>
  </svg>;
};

const PipelineBar = ({pipeline,scanCount})=>{
  const G="#00d4a0",B="#00b4ff",R="#ff5060",Y="#f0c040",V="#a78bfa";
  const stages=[
    {key:"scanning",label:"SCAN",  color:B,   val:pipeline.coin},
    {key:"signal",  label:"SIG",   color:V,   val:pipeline.score?`${pipeline.score}`:null},
    {key:"risk",    label:"RISK",  color:pipeline.result==="PASS"?G:R, val:pipeline.result},
    {key:"order",   label:"ORDER", color:pipeline.side==="LONG"?G:R,   val:pipeline.side||null},
    {key:"monitor", label:"MON",   color:Y,   val:null},
    {key:"exit",    label:"EXIT",  color:pipeline.exitReason?.includes("PROFIT")?G:pipeline.exitReason?R:B, val:pipeline.exitReason},
  ];
  return(
    <div style={{padding:"7px 13px",borderBottom:"1px solid rgba(0,180,255,0.09)",background:"#08131f",flexShrink:0}}>
      <div style={{display:"flex",alignItems:"center",gap:6,marginBottom:5}}>
        <Dot on={!!pipeline.active} size={5}/>
        <span style={{fontSize:8,letterSpacing:"1.5px",color:"rgba(0,180,255,0.5)",textTransform:"uppercase"}}>Auto Scalp Pipeline</span>
        <span style={{fontSize:8,color:"rgba(255,255,255,0.18)",marginLeft:"auto"}}>{scanCount} scans</span>
      </div>
      <div style={{display:"flex",alignItems:"flex-start",gap:0}}>
        {stages.map((s,i)=>(
          <div key={s.key} style={{display:"flex",alignItems:"center",flex:1,minWidth:0}}>
            <div style={{display:"flex",flexDirection:"column",alignItems:"center",gap:2,flex:1,minWidth:0}}>
              <div style={{width:7,height:7,borderRadius:"50%",flexShrink:0,
                background:pipeline.stage===s.key?s.color:"rgba(255,255,255,0.12)",
                boxShadow:pipeline.stage===s.key?`0 0 7px ${s.color}`:"none",
                animation:pipeline.stage===s.key?"pulse 1s infinite":"none"}}/>
              <div style={{fontSize:6.5,color:pipeline.stage===s.key?s.color:"rgba(255,255,255,0.2)",
                letterSpacing:"0.5px",textAlign:"center",overflow:"hidden",textOverflow:"ellipsis",
                whiteSpace:"nowrap",width:"100%",paddingInline:1}}>{s.label}</div>
              {s.val&&<div style={{fontSize:7,fontWeight:700,color:s.color,textAlign:"center",
                overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap",width:"100%",paddingInline:1}}>{s.val}</div>}
            </div>
            {i<stages.length-1&&<div style={{width:8,height:1,background:"rgba(255,255,255,0.07)",flexShrink:0,marginBottom:14}}/>}
          </div>
        ))}
      </div>
    </div>
  );
};

/* ══════════════════════════════════════════════════════════
   MAIN APP
   ══════════════════════════════════════════════════════════ */
export default function MakStanleyzLive() {
  /* ── state ── */
  const [connected,setConnected]   = useState(false);
  const [running,setRunning]       = useState(false);
  const [testnet,setTestnet]       = useState(true);
  const [hasKeys,setHasKeys]       = useState(false);
  const [apiKey,setApiKey]         = useState("");
  const [apiSecret,setApiSecret]   = useState("");
  const [lev,setLev]               = useState(5);
  const [sl,setSl]                 = useState("1.5");
  const [tp,setTp]                 = useState("3.0");
  const [freq,setFreq]             = useState(4);
  const [maxPos,setMaxPos]         = useState(5);
  const [sigThresh,setSigThresh]   = useState(65);
  const [activeStrats,setActiveStrats] = useState(STRATEGIES.map(s=>s.id));
  const [stats,setStats]           = useState({total_pnl:0,today_pnl:0,wins:0,losses:0,total_trades:0,balance:0,best_trade:0});
  const [positions,setPositions]   = useState([]);
  const [trades,setTrades]         = useState([]);
  const [prices,setPrices]         = useState({});
  const [pipeline,setPipeline]     = useState({stage:"",coin:"",score:null,side:"",result:"",exitReason:"",active:false});
  const [scanCount,setScanCount]   = useState(0);
  const [equityHist,setEquityHist] = useState([]);
  const [alerts,setAlerts]         = useState([]);
  const [tab,setTab]               = useState("positions");
  const [mobileTab,setMobileTab]   = useState("dashboard");
  const [isMobile,setIsMobile]     = useState(false);
  const [selCoin,setSelCoin]       = useState("BTCUSDT");
  const [query,setQuery]           = useState("");
  const [savingConfig,setSavingConfig] = useState(false);
  const wsRef = useRef(null);

  /* ── responsive ── */
  useEffect(()=>{
    const check=()=>setIsMobile(window.innerWidth<768);
    check(); window.addEventListener("resize",check);
    return()=>window.removeEventListener("resize",check);
  },[]);

  /* ── WebSocket connection ── */
  useEffect(()=>{
    let ws, retryTimer;
    const connect=()=>{
      ws=new WebSocket(BACKEND_WS);
      wsRef.current=ws;
      ws.onopen=()=>{setConnected(true);};
      ws.onclose=()=>{setConnected(false);retryTimer=setTimeout(connect,3000);};
      ws.onerror=()=>{ws.close();};
      ws.onmessage=e=>{
        try{
          const {event,data}=JSON.parse(e.data);
          if(event==="init"){
            setRunning(data.bot_running);
            setTestnet(data.testnet);
            setPositions(data.positions||[]);
            setStats(data.stats||{});
            setTrades(data.trades||[]);
            setPrices(data.prices||{});
            setActiveStrats(data.active_strats||STRATEGIES.map(s=>s.id));
          }
          else if(event==="pipeline"){
            setPipeline(p=>({...p,...data,active:true}));
            if(data.stage==="scanning") setScanCount(c=>c+1);
          }
          else if(event==="positions_update"){
            setPositions(data.positions||[]);
            setStats(s=>({...s,...data.stats}));
            setEquityHist(h=>{const nb=(data.stats?.balance||0);return nb?[...h.slice(-59),nb]:h;});
          }
          else if(event==="trade_closed"){
            setTrades(prev=>[data,...prev.slice(0,99)]);
            const pnl=data.pnl||0;
            const a={id:Date.now(),msg:`${data.win?"✓":"✗"} ${data.side} ${data.sym}: ${pnl>0?"+":""}$${Number(pnl).toFixed(2)}`,type:data.win?"win":"loss"};
            setAlerts(prev=>[a,...prev.slice(0,4)]);
            setTimeout(()=>setAlerts(prev=>prev.filter(x=>x.id!==a.id)),3500);
          }
          else if(event==="prices"){
            setPrices(data);
          }
          else if(event==="bot_status"){
            setRunning(data.running);
          }
        }catch{}
      };
    };
    connect();
    return()=>{clearTimeout(retryTimer);ws?.close();};
  },[]);

  /* ── load initial status ── */
  useEffect(()=>{
    api.get("/api/status").then(d=>{
      if(!d) return;
      setRunning(d.bot_running);
      setTestnet(d.testnet);
      setHasKeys(d.has_keys);
      setLev(d.leverage||5);
      setSl(String(d.sl_pct||"1.5"));
      setTp(String(d.tp_pct||"3.0"));
      setFreq(d.freq||4);
      setMaxPos(d.max_pos||5);
      setSigThresh(d.sig_thresh||65);
      setStats(d.stats||{});
      setPositions(d.open_positions||[]);
      setTrades(d.trade_history||[]);
      setPrices(d.price_cache||{});
      setActiveStrats(d.active_strats||STRATEGIES.map(s=>s.id));
    }).catch(()=>{});
  },[]);

  /* ── actions ── */
  const startBot = async()=>{
    const r=await api.post("/api/bot/start");
    if(r.ok) setRunning(true);
    else showAlert("error","Start failed: "+r.detail,"loss");
  };
  const stopBot = async()=>{
    await api.post("/api/bot/stop");
    setRunning(false);
  };
  const saveConfig = async()=>{
    setSavingConfig(true);
    try{
      await api.post("/api/config",{
        api_key: apiKey||undefined,
        api_secret: apiSecret||undefined,
        testnet, leverage:lev,
        trade_investment:50,
        stop_loss_pct:parseFloat(sl),
        take_profit_pct:parseFloat(tp),
        scan_interval_sec:freq,
        max_open_positions:maxPos,
        signal_threshold:sigThresh,
        active_strategies:activeStrats,
      });
      if(apiKey) setHasKeys(true);
      showAlert("Config saved ✓","win");
    }catch(e){showAlert("Save failed","loss");}
    setSavingConfig(false);
  };
  const closeAll=async()=>{
    if(!confirm("Close ALL open positions now?")) return;
    await api.post("/api/close_all");
  };
  const closePos=async(sym)=>{
    await api.post(`/api/trade/close/${sym}`);
  };
  const manualTrade=async(side)=>{
    await api.postQ("/api/trade/manual",{symbol:selCoin,side});
  };
  const showAlert=(msg,type)=>{
    const a={id:Date.now(),msg,type};
    setAlerts(prev=>[a,...prev.slice(0,4)]);
    setTimeout(()=>setAlerts(prev=>prev.filter(x=>x.id!==a.id)),3000);
  };
  const toggleStrat=id=>setActiveStrats(prev=>prev.includes(id)?prev.length>1?prev.filter(x=>x!==id):prev:[...prev,id]);

  /* ── computed ── */
  const wr = stats.total_trades>0?((stats.wins/stats.total_trades)*100).toFixed(1):"0.0";
  const filteredCoins = Object.keys(prices).filter(s=>s.includes(query.toUpperCase()));

  /* ── tokens ── */
  /* ── Sea-blue palette ───────────────────────────────────────────────────── */
  const G="#00E5A0",R="#FF4757",B="#29ABE2",Y="#FFD700";
  const BG0="#041523",BG1="#06253d",BG2="#083050",BD="rgba(41,171,226,0.22)";
  const inp={width:"100%",boxSizing:"border-box",background:"rgba(41,171,226,0.08)",
    border:`1px solid rgba(41,171,226,0.30)`,borderRadius:6,color:"#ffffff",
    fontSize:11,padding:"7px 10px",outline:"none",fontFamily:"inherit"};
  const blk={padding:"12px 14px",borderBottom:`1px solid ${BD}`};
  const lbl={fontSize:8,letterSpacing:"1.5px",color:"rgba(41,171,226,0.75)",textTransform:"uppercase",marginBottom:6};
  const pill=a=>({flex:1,minWidth:0,padding:"6px 4px",textAlign:"center",cursor:"pointer",
    background:a?B:"rgba(41,171,226,0.08)",
    border:a?`1px solid ${B}`:`1px solid rgba(41,171,226,0.22)`,
    color:a?"#ffffff":"rgba(255,255,255,0.40)",borderRadius:6,fontSize:10,fontWeight:700,transition:"all 0.14s"});

  /* ══ HEADER ══ */
  const Header=()=>(
    <div style={{background:"linear-gradient(90deg,#062a47,#083560)",borderBottom:`1px solid ${BD}`,
      padding:isMobile?"8px 12px":"10px 20px",display:"flex",alignItems:"center",
      justifyContent:"space-between",flexShrink:0,boxShadow:"0 2px 12px rgba(4,21,35,0.6)"}}>
      <div style={{display:"flex",alignItems:"center",gap:8}}>
        <span style={{fontSize:isMobile?17:20}}>🦈</span>
        <div>
          <div style={{fontSize:isMobile?14:17,fontWeight:900,letterSpacing:isMobile?1:3,
            background:"linear-gradient(90deg,#00b4ff,#00e5a0)",WebkitBackgroundClip:"text",WebkitTextFillColor:"transparent"}}>
            {BOT_NAME.toUpperCase()}
          </div>
          {!isMobile&&<div style={{fontSize:7,color:"rgba(0,180,255,0.3)",letterSpacing:"2px"}}>LIVE BINANCE FUTURES ENGINE v4.0</div>}
        </div>
      </div>
      <div style={{display:"flex",alignItems:"center",gap:isMobile?8:14}}>
        <div style={{display:"flex",alignItems:"center",gap:5}}>
          <Dot on={connected}/>
          <span style={{fontSize:8,color:connected?G:"rgba(255,255,255,0.3)",letterSpacing:"0.8px"}}>
            {connected?"WS LIVE":"DISCONNECTED"}
          </span>
        </div>
        {!isMobile&&(
          <div style={{fontSize:8,padding:"3px 8px",borderRadius:3,
            background:testnet?"rgba(240,192,64,0.12)":"rgba(255,80,96,0.12)",
            border:`1px solid ${testnet?"rgba(240,192,64,0.3)":"rgba(255,80,96,0.3)"}`,
            color:testnet?Y:R,fontWeight:700}}>
            {testnet?"TESTNET":"⚠ LIVE"}
          </div>
        )}
        <span style={{fontSize:10}}>
          <span style={{color:"rgba(0,180,255,0.4)"}}>BAL </span>
          <span style={{color:stats.balance>0?G:"rgba(255,255,255,0.4)",fontWeight:700}}>
            ${Number(stats.balance||0).toLocaleString()}
          </span>
        </span>
        <button onClick={running?stopBot:startBot} disabled={!hasKeys&&!running} style={{
          padding:"7px 18px",fontWeight:800,fontSize:11,cursor:hasKeys||running?"pointer":"not-allowed",
          borderRadius:7,letterSpacing:"1px",opacity:!hasKeys&&!running?0.45:1,
          background:running?R:B,border:"none",color:"#ffffff",
          boxShadow:running?`0 2px 10px rgba(255,71,87,0.5)`:`0 2px 10px rgba(41,171,226,0.5)`}}>
          {running?"⬛ STOP":"▶ START"}
        </button>
        {positions.length>0&&(
          <button onClick={closeAll} style={{padding:"7px 14px",fontWeight:800,fontSize:11,cursor:"pointer",
            borderRadius:7,background:R,border:"none",color:"#ffffff",
            boxShadow:"0 2px 10px rgba(255,71,87,0.4)"}}>
            ✕ CLOSE ALL
          </button>
        )}
      </div>
    </div>
  );

  /* ══ STATS CARDS ══ */
  const StatsCards=()=>{
    const cards=[
      {l:"Total P&L",  v:`${stats.total_pnl>=0?"+":""}$${Math.abs(stats.total_pnl||0).toFixed(2)}`, c:stats.total_pnl>=0?G:R},
      {l:"Today P&L",  v:`${stats.today_pnl>=0?"+":""}$${Math.abs(stats.today_pnl||0).toFixed(2)}`, c:stats.today_pnl>=0?G:R},
      {l:"Win Rate",   v:`${wr}%`, c:parseFloat(wr)>=70?G:parseFloat(wr)>=50?Y:R},
      {l:"Trades",     v:stats.total_trades||0, c:B},
      ...(!isMobile?[
        {l:"Open Pos",   v:`${positions.length}/${maxPos}`, c:positions.length>0?Y:B},
        {l:"Balance",    v:`$${Number(stats.balance||0).toLocaleString()}`, c:stats.balance>0?G:"rgba(255,255,255,0.5)"},
        {l:"Best Trade", v:`+$${Number(stats.best_trade||0).toFixed(2)}`, c:G},
        {l:"$/Trade",    v:"$50", c:Y},
      ]:[]),
    ];
    const cols=isMobile?4:8;
    return(
      <div style={{display:"grid",gridTemplateColumns:`repeat(${cols},1fr)`,flexShrink:0,borderBottom:`1px solid ${BD}`}}>
        {cards.map((s,i)=>(
          <div key={i} style={{padding:isMobile?"7px 8px":"8px 12px",borderRight:i<cols-1?`1px solid ${BD}`:"none"}}>
            <div style={{fontSize:7,color:"rgba(255,255,255,0.25)",letterSpacing:"1.2px",textTransform:"uppercase",marginBottom:2}}>{s.l}</div>
            <div style={{fontSize:isMobile?11:13,fontWeight:700,color:s.c}}>{s.v}</div>
          </div>
        ))}
      </div>
    );
  };

  /* ══ OPEN POSITIONS ══ */
  const PositionsPanel=()=>(
    <div style={{overflowY:"auto",flex:1}}>
      {positions.length===0?(
        <div style={{padding:24,textAlign:"center",color:"rgba(255,255,255,0.2)",fontSize:11}}>
          <div style={{fontSize:28,marginBottom:8}}>⚡</div>
          {running?"Scanning markets — positions will open shortly...":hasKeys?"Bot is paused. Press START to begin.":"Enter API keys in Settings first."}
        </div>
      ):positions.map(p=>{
        const upnl=parseFloat(p.unrealised_pnl||0);
        const age=Math.floor((Date.now()-p.entry_ts)/1000);
        const strat=STRATEGIES.find(s=>s.name===p.strategy);
        return(
          <div key={p.id} style={{padding:"9px 13px",borderBottom:`1px solid rgba(255,255,255,0.03)`,
            background:upnl>=0?"rgba(0,212,160,0.025)":"rgba(255,80,96,0.025)"}}>
            <div style={{display:"flex",justifyContent:"space-between",alignItems:"center",marginBottom:5}}>
              <div style={{display:"flex",alignItems:"center",gap:7,flexWrap:"wrap"}}>
                <span style={{fontSize:12,fontWeight:700,color:"#dde4f0"}}>{p.sym.replace("USDT","")}</span>
                <span style={{fontSize:8,fontWeight:700,padding:"1px 5px",borderRadius:3,
                  background:p.side==="LONG"?"rgba(0,212,160,0.12)":"rgba(255,80,96,0.12)",
                  color:p.side==="LONG"?G:R,border:`1px solid ${p.side==="LONG"?"rgba(0,212,160,0.25)":"rgba(255,80,96,0.25)"}`}}>
                  {p.side}
                </span>
                <span style={{fontSize:8,color:"rgba(0,180,255,0.6)"}}>{p.leverage}x</span>
                {strat&&<span style={{fontSize:8,color:strat.color,background:`${strat.color}18`,padding:"1px 5px",borderRadius:3}}>
                  {strat.name.split(" ")[0]}
                </span>}
                <span style={{fontSize:8,color:"rgba(255,255,255,0.25)"}}>{age}s</span>
              </div>
              <div style={{display:"flex",alignItems:"center",gap:8}}>
                <div style={{textAlign:"right"}}>
                  <div style={{fontSize:14,fontWeight:900,color:upnl>=0?G:R}}>
                    {upnl>=0?"+":""}${Math.abs(upnl).toFixed(2)}
                  </div>
                </div>
                <button onClick={()=>closePos(p.sym)} style={{padding:"5px 11px",fontSize:9,cursor:"pointer",
                  borderRadius:5,background:R,border:"none",color:"#fff",fontWeight:800,
                  boxShadow:"0 1px 6px rgba(255,71,87,0.4)"}}>
                  CLOSE
                </button>
              </div>
            </div>
            <div style={{height:3,borderRadius:2,background:"rgba(255,255,255,0.06)",overflow:"hidden",marginBottom:5}}>
              <div style={{height:"100%",borderRadius:2,
                background:upnl>=0?`linear-gradient(90deg,${G},rgba(0,212,160,0.4))`:`linear-gradient(90deg,${R},rgba(255,80,96,0.4))`,
                width:`${Math.min(100,Math.abs(upnl/p.investment*100*10))}%`,transition:"width 0.5s"}}/>
            </div>
            <div style={{display:"flex",justifyContent:"space-between",fontSize:8,color:"rgba(255,255,255,0.25)",flexWrap:"wrap",gap:4}}>
              <span>Entry: <span style={{color:"rgba(255,255,255,0.5)"}}>{fmtP(p.entry_price)}</span></span>
              <span>Now: <span style={{color:upnl>=0?G:R}}>{fmtP(p.current_price)}</span></span>
              <span>TP: <span style={{color:G}}>{fmtP(p.tp_target)}</span></span>
              <span>SL: <span style={{color:R}}>{fmtP(p.sl_target)}</span></span>
            </div>
          </div>
        );
      })}
    </div>
  );

  /* ══ TRADE HISTORY ROW ══ */
  const TradeRow=({t})=>{
    const pnl=parseFloat(t.pnl||0);
    const en=fmtDT(t.entry_ts||t.entryTs||Date.now());
    const ex=fmtDT(t.exit_ts||t.exitTs||Date.now());
    const strat=STRATEGIES.find(s=>s.name===t.strategy);
    if(isMobile){
      return(
        <div style={{padding:"8px 12px",borderBottom:"1px solid rgba(255,255,255,0.03)",
          background:pnl>0?"rgba(0,212,160,0.025)":"rgba(255,80,96,0.025)"}}>
          <div style={{display:"flex",justifyContent:"space-between",alignItems:"center",marginBottom:3}}>
            <div style={{display:"flex",alignItems:"center",gap:6}}>
              <span style={{fontSize:11,fontWeight:700,color:"#dde4f0"}}>{(t.sym||"").replace("USDT","")}</span>
              <span style={{fontSize:8,fontWeight:700,padding:"1px 5px",borderRadius:3,
                background:t.side==="LONG"?"rgba(0,212,160,0.12)":"rgba(255,80,96,0.12)",
                color:t.side==="LONG"?G:R}}>{t.side}</span>
              {strat&&<span style={{fontSize:8,color:strat.color}}>{strat.name.split(" ")[0]}</span>}
            </div>
            <span style={{fontSize:13,fontWeight:900,color:pnl>=0?G:R}}>{pnl>=0?"+":""}${Math.abs(pnl).toFixed(2)}</span>
          </div>
          <div style={{display:"grid",gridTemplateColumns:"1fr 1fr",gap:2}}>
            <span style={{fontSize:8,color:"rgba(255,255,255,0.28)"}}><span style={{color:`${B}88`}}>IN </span>{en.date} {en.time}</span>
            <span style={{fontSize:8,color:"rgba(255,255,255,0.28)"}}><span style={{color:`${R}88`}}>OUT </span>{ex.date} {ex.time}</span>
          </div>
        </div>
      );
    }
    return(
      <div style={{display:"grid",gridTemplateColumns:"58px 44px 52px 90px 90px 68px 44px 54px",
        padding:"5px 13px",fontSize:10,borderBottom:"1px solid rgba(255,255,255,0.02)",alignItems:"center",
        background:pnl>0?"rgba(0,212,160,0.018)":"rgba(255,80,96,0.018)"}}>
        <span style={{fontWeight:700,color:"#dde4f0"}}>{(t.sym||"").replace("USDT","")}</span>
        <span style={{fontSize:8,fontWeight:700,padding:"2px 4px",borderRadius:3,display:"inline-block",
          background:t.side==="LONG"?"rgba(0,212,160,0.1)":"rgba(255,80,96,0.1)",
          color:t.side==="LONG"?G:R}}>{t.side}</span>
        <span style={{fontSize:8,color:strat?.color||B,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}}>{(t.strategy||"").split(" ")[0]}</span>
        <div>
          <div style={{fontSize:9,color:B}}>{en.time}</div>
          <div style={{fontSize:7.5,color:"rgba(255,255,255,0.22)"}}>{en.date}</div>
        </div>
        <div>
          <div style={{fontSize:9,color:"rgba(255,80,96,0.75)"}}>{ex.time}</div>
          <div style={{fontSize:7.5,color:"rgba(255,255,255,0.22)"}}>{ex.date}</div>
        </div>
        <span style={{fontWeight:700,color:pnl>=0?G:R}}>{pnl>=0?"+":""}${Math.abs(pnl).toFixed(2)}</span>
        <span style={{fontSize:9,color:"rgba(240,192,64,0.7)"}}>$50</span>
        <span style={{fontSize:9,fontWeight:700,color:t.win?G:R}}>{t.win?"WIN":"LOSS"}</span>
      </div>
    );
  };

  /* ══ MARKETS PANEL ══ */
  const MarketsPanel=()=>{
    const coins=filteredCoins.slice(0,120);
    return(
      <div style={{display:"flex",flexDirection:"column",flex:1,overflow:"hidden"}}>
        <div style={{padding:"8px 12px",borderBottom:`1px solid ${BD}`,display:"flex",justifyContent:"space-between",flexShrink:0}}>
          <span style={{fontSize:9,fontWeight:700,letterSpacing:"2px",color:"rgba(0,180,255,0.5)",textTransform:"uppercase"}}>Markets</span>
          <span style={{fontSize:9,color:`${G}99`,fontWeight:700}}>{Object.keys(prices).length} live</span>
        </div>
        <input style={{margin:6,padding:"6px 10px",background:"rgba(0,180,255,0.04)",
          border:`1px solid rgba(0,180,255,0.14)`,borderRadius:5,color:"#dde4f0",
          fontSize:11,outline:"none",width:"calc(100% - 12px)",boxSizing:"border-box"}}
          placeholder="🔍  Search pairs..." value={query} onChange={e=>setQuery(e.target.value)}/>
        <div style={{overflowY:"auto",flex:1}}>
          {coins.map(sym=>{
            const p=prices[sym]||0;
            const hasOpen=positions.some(x=>x.sym===sym);
            return<div key={sym} style={{padding:"6px 12px",cursor:"pointer",
              background:sym===selCoin?"rgba(0,180,255,0.07)":"transparent",
              borderLeft:sym===selCoin?`2px solid ${B}`:"2px solid transparent",
              display:"flex",justifyContent:"space-between",alignItems:"center"}}
              onClick={()=>{setSelCoin(sym);if(isMobile)setMobileTab("dashboard");}}>
              <div>
                <div style={{display:"flex",alignItems:"center",gap:4}}>
                  <span style={{fontSize:11,fontWeight:700,color:"#dde4f0"}}>{sym.replace("USDT","")}</span>
                  {hasOpen&&<span style={{fontSize:7,background:"rgba(0,212,160,0.15)",color:G,padding:"0 4px",borderRadius:2,fontWeight:700}}>OPEN</span>}
                </div>
                <div style={{fontSize:8,color:"rgba(255,255,255,0.18)"}}>USDT-PERP</div>
              </div>
              <div style={{textAlign:"right"}}>
                <div style={{fontSize:10,fontWeight:700,color:G}}>{fmtP(p)}</div>
              </div>
            </div>;
          })}
        </div>
      </div>
    );
  };

  /* ══ RIGHT SETTINGS PANEL ══ */
  const SettingsPanel=()=>(
    <div style={{display:"flex",flexDirection:"column",overflow:"hidden",height:"100%"}}>
      <div style={{padding:"8px 12px",borderBottom:`1px solid ${BD}`,fontSize:9,fontWeight:700,
        letterSpacing:"2px",color:"rgba(0,180,255,0.5)",textTransform:"uppercase",flexShrink:0}}>
        Settings & Controls
      </div>
      <div style={{overflowY:"auto",flex:1}}>

        {/* Connection status */}
        <div style={{...blk,background:hasKeys?"rgba(0,212,160,0.04)":"rgba(255,80,96,0.04)"}}>
          <div style={{display:"flex",alignItems:"center",gap:10,marginBottom:8}}>
            <WinGauge rate={wr}/>
            <div>
              <div style={{fontSize:11,fontWeight:700,color:hasKeys?G:Y,marginBottom:2}}>
                {hasKeys?(running?"🟢 Bot Active":"⏸ Bot Ready"):"🔑 Keys Required"}
              </div>
              <div style={{fontSize:9,color:"rgba(255,255,255,0.35)"}}>
                {stats.wins||0} wins · {stats.losses||0} losses · {(WIN_RATE_TGT)}% target
              </div>
              <div style={{fontSize:9,color:Y,marginTop:2}}>
                {testnet?"🧪 Testnet Mode":"⚠️ LIVE Trading"} · $50/trade · {lev}x
              </div>
            </div>
          </div>
          <div style={{height:2,background:"rgba(255,255,255,0.05)",borderRadius:1,overflow:"hidden"}}>
            <div style={{height:"100%",background:`linear-gradient(90deg,${G},${B})`,
              width:`${Math.min(100,parseFloat(wr))}%`,transition:"width 0.5s",borderRadius:1}}/>
          </div>
        </div>

        {/* API Keys */}
        <div style={blk}>
          <div style={lbl}>🔑 Binance API Key</div>
          <input style={{...inp,marginBottom:7,fontFamily:"monospace"}} type="password"
            placeholder={hasKeys?"Key saved — enter to update...":"Paste API key here..."}
            value={apiKey} onChange={e=>setApiKey(e.target.value)} autoComplete="off"/>
          <div style={lbl}>🔒 API Secret</div>
          <input style={{...inp,marginBottom:8,fontFamily:"monospace"}} type="password"
            placeholder={hasKeys?"Secret saved — enter to update...":"Paste API secret here..."}
            value={apiSecret} onChange={e=>setApiSecret(e.target.value)} autoComplete="off"/>
          <div style={{display:"flex",gap:8,alignItems:"center",marginBottom:8}}>
            <div style={{display:"flex",alignItems:"center",gap:6,flex:1}}>
              <div style={{...pill(testnet),fontSize:9}} onClick={()=>setTestnet(true)}>🧪 Testnet</div>
              <div style={{...pill(!testnet),fontSize:9,color:!testnet?R:undefined,border:!testnet?`1px solid rgba(255,80,96,0.5)`:undefined}}
                onClick={()=>setTestnet(false)}>⚠ Live</div>
            </div>
          </div>
          <button onClick={saveConfig} disabled={savingConfig} style={{width:"100%",padding:"10px",
            background:savingConfig?"rgba(41,171,226,0.5)":B,border:"none",color:"#ffffff",
            borderRadius:7,fontSize:12,fontWeight:800,cursor:"pointer",letterSpacing:"1px",
            boxShadow:"0 2px 12px rgba(41,171,226,0.45)"}}>
            {savingConfig?"SAVING...":"💾 SAVE CONFIG"}
          </button>
          {!hasKeys&&<div style={{marginTop:8,padding:"7px 10px",background:"rgba(240,192,64,0.06)",
            border:`1px solid rgba(240,192,64,0.15)`,borderRadius:4,fontSize:8.5,color:"rgba(240,192,64,0.7)",lineHeight:1.65}}>
            ⚠️ Start on Testnet first. Get testnet keys at testnet.binancefuture.com → Enable Futures + Read permissions only. IP-restrict your live keys.
          </div>}
        </div>

        {/* Leverage */}
        <div style={blk}>
          <div style={lbl}>Leverage: <span style={{color:B}}>{lev}x</span>
            <span style={{color:"rgba(255,255,255,0.2)",fontSize:8,marginLeft:6}}>→ ${50*lev} exposure/trade</span>
          </div>
          <div style={{display:"flex",gap:5}}>
            {[1,2,3,5,7,10].map(l=><div key={l} style={pill(lev===l)} onClick={()=>setLev(l)}>{l}x</div>)}
          </div>
        </div>

        {/* Frequency */}
        <div style={blk}>
          <div style={lbl}>Scan Frequency: <span style={{color:B}}>every {freq}s</span></div>
          <div style={{display:"flex",gap:5}}>
            {[2,3,5,8,12,20].map(s=><div key={s} style={pill(freq===s)} onClick={()=>setFreq(s)}>{s}s</div>)}
          </div>
        </div>

        {/* SL / TP */}
        <div style={blk}>
          <div style={{display:"grid",gridTemplateColumns:"1fr 1fr",gap:8}}>
            <div>
              <div style={lbl}>Stop Loss %</div>
              <input style={inp} type="number" step="0.1" value={sl} onChange={e=>setSl(e.target.value)}/>
            </div>
            <div>
              <div style={lbl}>Take Profit %</div>
              <input style={inp} type="number" step="0.1" value={tp} onChange={e=>setTp(e.target.value)}/>
            </div>
          </div>
          <div style={{marginTop:6,fontSize:8,color:"rgba(255,255,255,0.25)"}}>
            R:R <span style={{color:Y}}>{(parseFloat(tp||1)/parseFloat(sl||1)).toFixed(2)}</span>
            &nbsp;· Max positions: <span style={{color:B}}>{maxPos}</span>
            &nbsp;· Min signal: <span style={{color:B}}>{sigThresh}</span>
          </div>
        </div>

        {/* Advanced */}
        <div style={blk}>
          <div style={lbl}>Advanced</div>
          <div style={{display:"grid",gridTemplateColumns:"1fr 1fr",gap:8}}>
            <div>
              <div style={lbl}>Max Positions</div>
              <input style={inp} type="number" min="1" max="10" value={maxPos} onChange={e=>setMaxPos(Number(e.target.value))}/>
            </div>
            <div>
              <div style={lbl}>Signal Threshold</div>
              <input style={inp} type="number" min="50" max="95" value={sigThresh} onChange={e=>setSigThresh(Number(e.target.value))}/>
            </div>
          </div>
        </div>

        {/* Strategy manager */}
        <div style={blk}>
          <div style={lbl}>Active Strategies ({activeStrats.length}/{STRATEGIES.length})</div>
          <div style={{display:"flex",flexDirection:"column",gap:4}}>
            {STRATEGIES.map(s=>{
              const on=activeStrats.includes(s.id);
              return(
                <div key={s.id} onClick={()=>toggleStrat(s.id)} style={{
                  display:"flex",alignItems:"center",gap:8,padding:"5px 8px",borderRadius:4,cursor:"pointer",
                  background:on?`${s.color}10`:"rgba(255,255,255,0.02)",
                  border:`1px solid ${on?s.color+"40":"rgba(255,255,255,0.05)"}`,transition:"all 0.12s"}}>
                  <div style={{width:6,height:6,borderRadius:"50%",background:on?s.color:"rgba(255,255,255,0.15)",flexShrink:0}}/>
                  <span style={{flex:1,fontSize:9,fontWeight:700,color:on?"#dde4f0":"rgba(255,255,255,0.35)"}}>{s.name}</span>
                  <span style={{fontSize:8,fontWeight:700,color:on?s.color:"rgba(255,255,255,0.2)"}}>{on?"ON":"OFF"}</span>
                </div>
              );
            })}
          </div>
        </div>

        {/* Manual order */}
        <div style={blk}>
          <div style={lbl}>Manual Order — {selCoin.replace("USDT","")}</div>
          <div style={{display:"flex",gap:7}}>
            <button style={{flex:1,padding:"10px",background:G,border:"none",
              color:"#041523",borderRadius:7,fontSize:12,fontWeight:800,cursor:"pointer",
              boxShadow:"0 2px 12px rgba(0,229,160,0.4)"}}
              onClick={()=>manualTrade("LONG")}>▲ LONG</button>
            <button style={{flex:1,padding:"10px",background:R,border:"none",
              color:"#ffffff",borderRadius:7,fontSize:12,fontWeight:800,cursor:"pointer",
              boxShadow:"0 2px 12px rgba(255,71,87,0.4)"}}
              onClick={()=>manualTrade("SHORT")}>▼ SHORT</button>
          </div>
        </div>

        {/* Equity curve */}
        <div style={blk}>
          <div style={lbl}>Equity Curve</div>
          <Spark data={equityHist.length?equityHist:[10000]} color={B} h={44}/>
          <div style={{display:"flex",justifyContent:"space-between",fontSize:8,color:"rgba(255,255,255,0.2)",marginTop:4}}>
            <span>$10,000</span>
            <span style={{color:stats.balance>10000?G:R,fontWeight:700}}>${Number(stats.balance||10000).toLocaleString()}</span>
          </div>
        </div>
      </div>
    </div>
  );

  /* ══ CENTER PANEL ══ */
  const CenterPanel=()=>(
    <div style={{display:"flex",flexDirection:"column",flex:1,overflow:"hidden"}}>
      {/* Selected coin info */}
      <div style={{padding:isMobile?"8px 12px":"9px 16px",borderBottom:`1px solid ${BD}`,background:BG2,flexShrink:0}}>
        <div style={{display:"flex",alignItems:"center",justifyContent:"space-between",flexWrap:"wrap",gap:6}}>
          <div>
            <div style={{fontSize:9,color:"rgba(0,180,255,0.42)",letterSpacing:"1.8px",marginBottom:1}}>{selCoin} PERPETUAL</div>
            <div style={{fontSize:isMobile?20:24,fontWeight:900,color:G,
              textShadow:"0 0 14px rgba(0,212,160,0.3)"}}>{fmtP(prices[selCoin])}</div>
          </div>
          <div style={{display:"flex",flexWrap:"wrap",gap:isMobile?10:16}}>
            {[
              {l:"PAIRS",  v:Object.keys(prices).length+" live", c:"rgba(0,180,255,0.6)"},
              {l:"OPEN",   v:`${positions.length}/${maxPos}`,     c:positions.length>0?Y:B},
              {l:"NET P&L",v:`${stats.total_pnl>=0?"+":""}$${Math.abs(stats.total_pnl||0).toFixed(2)}`, c:stats.total_pnl>=0?G:R},
              {l:"$/TRADE",v:"$50",                               c:"rgba(0,212,160,0.75)"},
            ].map(x=><div key={x.l}>
              <div style={{fontSize:7,color:"rgba(255,255,255,0.2)",letterSpacing:"0.8px"}}>{x.l}</div>
              <div style={{fontSize:10,fontWeight:700,color:x.c}}>{x.v}</div>
            </div>)}
          </div>
        </div>
      </div>

      {/* Pipeline */}
      <PipelineBar pipeline={pipeline} scanCount={scanCount}/>

      {/* Stats */}
      <StatsCards/>

      {/* Tabs */}
      <div style={{display:"flex",borderBottom:`1px solid ${BD}`,background:BG2,flexShrink:0}}>
        {[
          {id:"positions",label:`Positions (${positions.length})`},
          {id:"history",  label:`History (${trades.length})`},
        ].map(t=>(
          <div key={t.id} style={{padding:"7px 14px",fontSize:9,letterSpacing:"1.5px",textTransform:"uppercase",cursor:"pointer",
            color:tab===t.id?B:"rgba(255,255,255,0.27)",borderBottom:tab===t.id?`2px solid ${B}`:"2px solid transparent"}}
            onClick={()=>setTab(t.id)}>
            {t.label}
          </div>
        ))}
      </div>

      {/* Tab content */}
      <div style={{overflowY:"auto",flex:1}}>
        {tab==="positions"&&<PositionsPanel/>}
        {tab==="history"&&<>
          {!isMobile&&<div style={{display:"grid",gridTemplateColumns:"58px 44px 52px 90px 90px 68px 44px 54px",
            padding:"6px 13px",fontSize:8,letterSpacing:"1.4px",color:"rgba(255,255,255,0.2)",textTransform:"uppercase",
            borderBottom:`1px solid rgba(0,180,255,0.04)`,position:"sticky",top:0,background:BG2,zIndex:1}}>
            <span>PAIR</span><span>SIDE</span><span>STRAT</span><span>ENTRY</span><span>EXIT</span><span>P&amp;L</span><span>INV</span><span>RESULT</span>
          </div>}
          {trades.map((t,i)=><TradeRow key={t.id||i} t={t}/>)}
        </>}
      </div>
    </div>
  );

  /* ══ MOBILE NAV ══ */
  const MobileNav=()=>(
    <div style={{display:"flex",background:BG1,borderTop:`1px solid ${BD}`,flexShrink:0}}>
      {[{id:"dashboard",icon:"📊",label:"Dashboard"},{id:"markets",icon:"🔍",label:"Markets"},{id:"settings",icon:"⚙️",label:"Settings"}].map(n=>(
        <div key={n.id} onClick={()=>setMobileTab(n.id)} style={{flex:1,padding:"8px 0",textAlign:"center",cursor:"pointer",
          background:mobileTab===n.id?"rgba(0,180,255,0.07)":"transparent",
          borderTop:mobileTab===n.id?`2px solid ${B}`:"2px solid transparent"}}>
          <div style={{fontSize:15}}>{n.icon}</div>
          <div style={{fontSize:8,color:mobileTab===n.id?B:"rgba(255,255,255,0.32)",marginTop:2}}>{n.label}</div>
        </div>
      ))}
    </div>
  );

  /* ══ ROOT ══ */
  return(
    <div style={{fontFamily:"'Courier New',Consolas,monospace",background:BG0,
      height:"100vh",color:"#ffffff",overflow:"hidden",display:"flex",flexDirection:"column"}}>
      <style>{`
        @keyframes pulse{0%,100%{opacity:1}50%{opacity:0.25}}
        @keyframes slideIn{from{transform:translateX(14px);opacity:0}to{transform:translateX(0);opacity:1}}
        *::-webkit-scrollbar{width:4px}
        *::-webkit-scrollbar-thumb{background:rgba(41,171,226,0.35);border-radius:3px}
        *::-webkit-scrollbar-track{background:rgba(41,171,226,0.05)}
        input::placeholder{color:rgba(255,255,255,0.25)}
        button:hover{filter:brightness(1.12);transform:translateY(-1px)}
        button:active{transform:translateY(0px)}
        button{transition:all 0.15s ease}
      `}</style>

      {/* Toasts */}
      <div style={{position:"fixed",top:isMobile?54:57,right:10,zIndex:500,width:isMobile?210:260,pointerEvents:"none"}}>
        {alerts.map(a=>(
          <div key={a.id} style={{background:a.type==="win"?"rgba(0,22,14,0.97)":"rgba(24,0,6,0.97)",
            border:`1px solid ${a.type==="win"?"rgba(0,212,160,0.5)":"rgba(255,80,96,0.5)"}`,
            borderRadius:6,padding:"8px 12px",fontSize:10,color:a.type==="win"?G:R,
            fontWeight:700,marginBottom:5,animation:"slideIn 0.22s ease",lineHeight:1.4}}>
            {a.msg}
          </div>
        ))}
      </div>

      <Header/>

      {/* Desktop */}
      {!isMobile&&(
        <div style={{display:"grid",gridTemplateColumns:"220px 1fr 270px",flex:1,overflow:"hidden",minHeight:0}}>
          <div style={{background:BG1,borderRight:`1px solid ${BD}`,overflow:"hidden",display:"flex",flexDirection:"column"}}><MarketsPanel/></div>
          <div style={{background:BG0,display:"flex",flexDirection:"column",overflow:"hidden",borderRight:`1px solid ${BD}`}}><CenterPanel/></div>
          <div style={{background:BG1,overflow:"hidden",display:"flex",flexDirection:"column"}}><SettingsPanel/></div>
        </div>
      )}

      {/* Mobile */}
      {isMobile&&(
        <>
          <div style={{flex:1,overflow:"hidden",display:"flex",flexDirection:"column",minHeight:0}}>
            {mobileTab==="dashboard"&&<CenterPanel/>}
            {mobileTab==="markets"  &&<div style={{display:"flex",flexDirection:"column",flex:1,overflow:"hidden",background:BG1}}><MarketsPanel/></div>}
            {mobileTab==="settings" &&<div style={{display:"flex",flexDirection:"column",flex:1,overflow:"hidden",background:BG1}}><SettingsPanel/></div>}
          </div>
          <MobileNav/>
        </>
      )}
    </div>
  );
}
