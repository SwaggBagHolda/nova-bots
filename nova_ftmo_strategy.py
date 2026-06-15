#!/usr/bin/env python3
"""
Nova FTMO Live Paper Trader — XAU/USD + EUR/USD + GBP/USD
Real 5-min intraday bars via Yahoo Finance (free, no key, live)
Strategy: London/NY session breakout
  - 20-bar high/low breakout on prior candles (zero lookahead)
  - ATR-based stop + 2:1 RR
  - EMA trend filter
FTMO Phase 1 rules hard-enforced:
  - 1% risk per trade
  - 5% max daily loss
  - 10% max total drawdown
  - Min 4 trading days
  - No weekend holds
"""
import urllib.request, json, time, os, requests
from datetime import datetime, timezone

PAIRS = {
    "XAUUSD": "GC=F",       # Gold futures — tracks XAU/USD
    "EURUSD": "EURUSD=X",
    "GBPUSD": "GBPUSD=X",
}
ACCOUNT       = 10_000.0
PROFIT_TARGET = 0.10
MAX_TOTAL_DD  = 0.10
MAX_DAILY_DD  = 0.05
RISK_PCT      = 0.01
RR            = 2.0
ATR_MULT      = 1.5
MAX_OPEN      = 2
SCAN_SEC      = 120
MIN_BARS      = 25

B44     = os.environ.get("BASE44_SERVICE_TOKEN","")
B44_URL = "https://api.base44.com/api/apps/69bf82ce8c526c379bdab3ce/entities/ScalperTrade"

equity      = ACCOUNT
peak_equity = ACCOUNT
daily_start = ACCOUNT
trade_days  = set()
positions   = {}
phase       = 1
passed      = False
last_day    = None

def log(m): print(f"[FTMO {datetime.now(timezone.utc).strftime('%H:%M')}] {m}", flush=True)

def log_db(sym, sig, entry, ex, pnl_usd, pnl_pct, status, reason):
    if not B44: return
    try:
        requests.post(B44_URL, json={
            "bot_name":"FTMO Live Paper","symbol":sym,
            "scan_time":datetime.now(timezone.utc).isoformat(),
            "signal":sig,"entry_price":round(entry,4),"exit_price":round(ex,4),
            "pnl_usd":round(pnl_usd,2),"pnl_pct":round(pnl_pct,4),
            "trade_status":status,"reason":reason
        }, headers={"Authorization":f"Bearer {B44}","Content-Type":"application/json"}, timeout=8)
    except: pass

def get_bars(ticker, n=60):
    """Real 5-min bars from Yahoo Finance — free, live, no key needed"""
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=5m&range=2d"
        req = urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            d = json.loads(resp.read())
        result = d["chart"]["result"][0]
        closes = result["indicators"]["quote"][0]["close"]
        highs  = result["indicators"]["quote"][0]["high"]
        lows   = result["indicators"]["quote"][0]["low"]
        vols   = result["indicators"]["quote"][0].get("volume",[])
        bars = []
        for i in range(len(closes)):
            if closes[i] and highs[i] and lows[i]:
                bars.append({"c":closes[i],"h":highs[i],"l":lows[i],
                             "v":vols[i] if vols and i<len(vols) and vols[i] else 0})
        return bars[-n:] if len(bars) >= n else bars
    except Exception as e:
        log(f"get_bars {ticker} error: {e}")
        return []

def get_signal(bars):
    """
    20-bar breakout — prior bars ONLY, no lookahead.
    Long:  close breaks above prior 20-bar high + above EMA20 + RSI < 68
    Short: close breaks below prior 20-bar low  + below EMA20 + RSI > 32
    """
    if len(bars) < MIN_BARS: return None, None, None
    prior  = bars[:-1]
    curr   = bars[-1]["c"]
    hi20   = max(b["h"] for b in prior[-20:])
    lo20   = min(b["l"] for b in prior[-20:])
    closes = [b["c"] for b in prior]
    # EMA20
    k=2/21; ema=closes[0]
    for c in closes[1:]: ema=c*k+ema*(1-k)
    # RSI14
    d=[closes[i+1]-closes[i] for i in range(len(closes)-1)]
    g=[max(x,0) for x in d[-14:]]; l=[abs(min(x,0)) for x in d[-14:]]
    ag,al=sum(g)/14,sum(l)/14
    rsi=100 if al==0 else 100-(100/(1+ag/al))
    # ATR14
    trs=[max(prior[i]["h"]-prior[i]["l"],
             abs(prior[i]["h"]-prior[i-1]["c"]),
             abs(prior[i]["l"]-prior[i-1]["c"])) for i in range(1,len(prior))]
    atr=sum(trs[-14:])/14 if len(trs)>=14 else None
    if not atr or atr<=0: return None, None, None
    signal=None
    if curr>hi20 and curr>ema and rsi<68:  signal="long"
    elif curr<lo20 and curr<ema and rsi>32: signal="short"
    return signal, atr, curr

def is_session():
    """London 3–8 AM ET (7–12 UTC) | NY 8 AM–12 PM ET (12–16 UTC)"""
    now=datetime.now(timezone.utc)
    m=now.hour*60+now.minute
    return 7*60<=m<=16*60 and now.weekday()<5

def is_friday_eod():
    now=datetime.now(timezone.utc)
    return now.weekday()==4 and now.hour>=19

def within_rules():
    total_dd=(ACCOUNT-equity)/ACCOUNT
    daily_dd=(daily_start-equity)/max(daily_start,1)
    if total_dd>=MAX_TOTAL_DD: log(f"🚨 TOTAL DD {total_dd*100:.1f}% BREACHED"); return False
    if daily_dd>=MAX_DAILY_DD: log(f"🚨 DAILY DD {daily_dd*100:.1f}% BREACHED"); return False
    return True

def check_phase():
    global phase, passed
    profit=(equity-ACCOUNT)/ACCOUNT
    target=PROFIT_TARGET if phase==1 else 0.05
    if profit>=target and len(trade_days)>=4:
        log(f"🏆 PHASE {phase} PASSED — profit={profit*100:.1f}% days={len(trade_days)}")
        if phase==1: phase=2; log("→ Phase 2: target 5%")
        else: passed=True; log("🎉 FTMO CHALLENGE COMPLETE — ready for live account!")

def close_trade(sym, reason, price):
    global equity, peak_equity
    pos=positions.pop(sym,None)
    if not pos: return
    if pos["side"]=="long":  pnl=((price-pos["entry"])/pos["atr"])*pos["risk_usd"]/ATR_MULT
    else:                    pnl=((pos["entry"]-price)/pos["atr"])*pos["risk_usd"]/ATR_MULT
    equity+=pnl; peak_equity=max(peak_equity,equity)
    pct=pnl/ACCOUNT*100; status="WIN" if pnl>0 else "LOSS"
    total_p=(equity-ACCOUNT)/ACCOUNT*100
    log(f"{'✅' if pnl>0 else '❌'} CLOSED {sym} {pos['side'].upper()} | {reason} | ${pnl:+.2f} ({pct:+.2f}%) | equity=${equity:.0f} total={total_p:+.2f}%")
    log_db(sym,pos["side"].upper(),pos["entry"],price,pnl,pct,status,reason)
    check_phase()

def run():
    global daily_start, last_day
    log("=== Nova FTMO Live Paper — Phase 1 ===")
    log(f"Pairs: XAU/USD | EUR/USD | GBP/USD")
    log(f"${ACCOUNT:.0f} | Target +10% | MaxDD 10% | Daily -5% | 1% risk | 2:1 RR")
    log("Signal: 20-bar breakout + EMA20 trend + RSI filter | London/NY sessions only")
    while True:
        global last_day
        today=datetime.now().strftime("%Y-%m-%d")
        if today!=last_day:
            daily_start=equity; last_day=today
            log(f"📅 Day reset — equity=${equity:.0f} profit={(equity-ACCOUNT)/ACCOUNT*100:+.2f}%")

        if passed: time.sleep(3600); continue
        if not within_rules(): time.sleep(3600); continue

        if is_friday_eod():
            for sym in list(positions.keys()): 
                bars=get_bars(PAIRS[sym])
                close_trade(sym,"WEEKEND_CLOSE",bars[-1]["c"] if bars else positions[sym]["entry"])
            time.sleep(3600); continue

        trade_days.add(today)

        # Manage open positions
        for sym in list(positions.keys()):
            bars=get_bars(PAIRS[sym])
            if not bars: continue
            price=bars[-1]["c"]; pos=positions[sym]
            bars_held=pos.get("bars",0)+1; positions[sym]["bars"]=bars_held
            hit_stop= price<=pos["stop"] if pos["side"]=="long" else price>=pos["stop"]
            hit_tgt=  price>=pos["target"] if pos["side"]=="long" else price<=pos["target"]
            live_pnl=((price-pos["entry"]) if pos["side"]=="long" else (pos["entry"]-price))
            log(f"  HOLD {sym} {pos['side'].upper()} bar={bars_held} price={price:.4f} live={live_pnl:+.4f}")
            if hit_stop:        close_trade(sym,"STOP",price)
            elif hit_tgt:       close_trade(sym,"TARGET",price)
            elif bars_held>=48: close_trade(sym,"TIMEOUT",price)
            time.sleep(0.5)

        # Entries — session hours only
        if is_session() and len(positions)<MAX_OPEN:
            for sym,ticker in PAIRS.items():
                if sym in positions: continue
                if len(positions)>=MAX_OPEN: break
                if not within_rules(): break
                bars=get_bars(ticker)
                if not bars or len(bars)<MIN_BARS: continue
                signal,atr,price=get_signal(bars)
                if not signal: continue
                risk_usd=equity*RISK_PCT
                stop_d=atr*ATR_MULT
                stop_p=(price-stop_d) if signal=="long" else (price+stop_d)
                tgt_p= (price+stop_d*RR) if signal=="long" else (price-stop_d*RR)
                positions[sym]={"entry":price,"side":signal,"stop":stop_p,"target":tgt_p,
                                "risk_usd":risk_usd,"atr":atr,"bars":0}
                p_pct=(equity-ACCOUNT)/ACCOUNT*100
                dd=(peak_equity-equity)/peak_equity*100 if peak_equity>0 else 0
                log(f"📈 ENTERED {sym} {signal.upper()} @ {price:.4f} | stop={stop_p:.4f} tgt={tgt_p:.4f} risk=${risk_usd:.0f} | profit={p_pct:+.2f}% dd={dd:.2f}%")
                time.sleep(0.5)
        time.sleep(SCAN_SEC)

if __name__=="__main__":
    run()
