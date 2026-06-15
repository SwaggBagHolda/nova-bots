import nova_logger
#!/usr/bin/env python3
"""
Nova Forex Live Scalper — 24/7 Paper Trading
Pairs: EUR/USD, GBP/USD, XAU/USD, USD/JPY, AUD/USD, GBP/JPY
Real 5-min bars via Yahoo Finance — no API key needed
Strategy: Session momentum scalper
  - London open (7–10 UTC): breakout entries
  - NY session (12–16 UTC): trend continuation
  - Asia (22–06 UTC): range fade on USD/JPY + AUD/USD
Signal: 20-bar breakout + EMA trend + RSI + ATR-based R:R
Risk: 1% per trade, 2:1 R:R, max 3 open positions
Logs all trades to Base44 DB
"""
import urllib.request, json, time, os, requests
from datetime import datetime, timezone

PAIRS = {
    "XAUUSD": "GC=F",
    "EURUSD": "EURUSD=X",
    "GBPUSD": "GBPUSD=X",
    "USDJPY": "USDJPY=X",
    "AUDUSD": "AUDUSD=X",
    "GBPJPY": "GBPJPY=X",
}
# Which pairs trade which session
SESSIONS = {
    "london": ["XAUUSD","EURUSD","GBPUSD","GBPJPY"],
    "ny":     ["XAUUSD","EURUSD","GBPUSD","USDJPY"],
    "asia":   ["USDJPY","AUDUSD","GBPJPY"],
}
ACCOUNT    = 10_000.0
RISK_PCT   = 0.01
RR         = 2.0
ATR_MULT   = 1.5
MAX_OPEN   = 3
SCAN_SEC   = 120
MIN_BARS   = 25

B44     = os.environ.get("BASE44_SERVICE_TOKEN","eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiI1NTBiODExZS1kMGNlLTRkYjAtODVmZS0wYTE4MzZiZTVmNDciLCJjbGllbnRfaWQiOiI1NTBiODExZS1kMGNlLTRkYjAtODVmZS0wYTE4MzZiZTVmNDciLCJhcHBfaWQiOiI2OWJmODJjZThjNTI2YzM3OWJkYWIzY2UiLCJhdWQiOiJiYXNlNDRfYXBpIiwic2NvcGUiOiJhcHAuYWNjZXNzIiwiZXhwIjoxNzgxNTQ4MDI4LCJpYXQiOjE3ODE1NDQ0Mjh9.-j7b7k8WevY27CMESHTC-qpSuRjJfQGc4exMg9sL5aE")
B44_URL = "https://app.base44.com/api/apps/69bf82ce8c526c379bdab3ce/entities/ScalperTrade"

equity      = ACCOUNT
peak_equity = ACCOUNT
positions   = {}
daily_pnl   = 0.0
last_day    = None

def log(m): print(f"[FOREX {datetime.now(timezone.utc).strftime('%H:%M')}] {m}", flush=True)

def log_db(sym, sig, entry, ex, pnl_usd, pnl_pct, status, reason):
    if not B44: return
    try:
        requests.post(B44_URL, json={
            "bot_name":"Forex Live Scalper","symbol":sym,
            "scan_time":datetime.now(timezone.utc).isoformat(),
            "signal":sig,"entry_price":round(entry,5),"exit_price":round(ex,5),
            "pnl_usd":round(pnl_usd,2),"pnl_pct":round(pnl_pct,4),
            "trade_status":status,"reason":reason
        }, headers={"Authorization":f"Bearer {B44}","Content-Type":"application/json"}, timeout=8)
    except: pass

def get_bars(ticker, n=40):
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=5m&range=2d"
        req = urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            d = json.loads(resp.read())
        result = d["chart"]["result"][0]
        closes = result["indicators"]["quote"][0]["close"]
        highs  = result["indicators"]["quote"][0]["high"]
        lows   = result["indicators"]["quote"][0]["low"]
        bars = [{"c":closes[i],"h":highs[i],"l":lows[i]}
                for i in range(len(closes)) if closes[i] and highs[i] and lows[i]]
        return bars[-n:] if len(bars)>=n else bars
    except: return []

def get_signal(bars, session="london"):
    if len(bars)<MIN_BARS: return None,None,None
    prior  = bars[:-1]
    curr   = bars[-1]["c"]
    closes = [b["c"] for b in prior]
    hi20   = max(b["h"] for b in prior[-20:])
    lo20   = min(b["l"] for b in prior[-20:])
    # EMA20 on prior
    k=2/21; ema=closes[0]
    for c in closes[1:]: ema=c*k+ema*(1-k)
    # RSI14 on prior
    d=[closes[i+1]-closes[i] for i in range(len(closes)-1)]
    g=[max(x,0) for x in d[-14:]]; l=[abs(min(x,0)) for x in d[-14:]]
    ag,al=sum(g)/14,sum(l)/14
    rsi=100 if al==0 else 100-(100/(1+ag/al))
    # ATR14 on prior
    trs=[max(prior[i]["h"]-prior[i]["l"],
             abs(prior[i]["h"]-prior[i-1]["c"]),
             abs(prior[i]["l"]-prior[i-1]["c"])) for i in range(1,len(prior))]
    atr=sum(trs[-14:])/14 if len(trs)>=14 else None
    if not atr or atr<=0: return None,None,None

    if session=="asia":
        # Asia: fade range extremes (mean reversion) — RSI overbought/oversold
        if rsi>68 and curr>=hi20*0.999: signal="short"
        elif rsi<32 and curr<=lo20*1.001: signal="long"
        else: signal=None
    else:
        # London/NY: breakout momentum
        if curr>hi20 and curr>ema and rsi<68: signal="long"
        elif curr<lo20 and curr<ema and rsi>32: signal="short"
        else: signal=None

    return signal, atr, curr

def get_session():
    m = datetime.now(timezone.utc).hour*60 + datetime.now(timezone.utc).minute
    if 7*60<=m<10*60:  return "london"
    if 12*60<=m<16*60: return "ny"
    if m>=22*60 or m<6*60: return "asia"
    return None

def is_friday_eod():
    now=datetime.now(timezone.utc)
    return now.weekday()==4 and now.hour>=19

def close_trade(sym, reason, price):
    global equity, peak_equity, daily_pnl
    pos=positions.pop(sym,None)
    if not pos: return
    if pos["side"]=="long":  pnl=((price-pos["entry"])/pos["atr"])*pos["risk_usd"]/ATR_MULT
    else:                    pnl=((pos["entry"]-price)/pos["atr"])*pos["risk_usd"]/ATR_MULT
    equity+=pnl; peak_equity=max(peak_equity,equity); daily_pnl+=pnl
    pct=pnl/ACCOUNT*100; status="WIN" if pnl>0 else "LOSS"
    total_p=(equity-ACCOUNT)/ACCOUNT*100
    log(f"{'✅' if pnl>0 else '❌'} CLOSED {sym} {pos['side'].upper()} | {reason} | ${pnl:+.2f} ({pct:+.2f}%) | equity=${equity:.0f} total={total_p:+.2f}%")
    nova_logger.log_trade('Forex Live Scalper',sym,pos['side'].upper(),pos['entry'],price,pnl,pct,status,reason)

def run():
    global last_day, daily_pnl
    log("=== Nova Forex Live Scalper — STARTING ===")
    log("Pairs: XAU/USD | EUR/USD | GBP/USD | USD/JPY | AUD/USD | GBP/JPY")
    log("Sessions: London (3-6 AM ET) | NY (8 AM-12 PM ET) | Asia (10 PM-2 AM ET)")
    while True:
        today=datetime.now().strftime("%Y-%m-%d")
        if today!=last_day:
            daily_pnl=0.0; last_day=today
            log(f"📅 New day — equity=${equity:.0f} | total={(equity-ACCOUNT)/ACCOUNT*100:+.2f}%")

        if is_friday_eod():
            for sym in list(positions.keys()):
                bars=get_bars(PAIRS[sym])
                close_trade(sym,"WEEKEND_CLOSE",bars[-1]["c"] if bars else positions[sym]["entry"])
            time.sleep(3600); continue

        session=get_session()

        # Manage open positions (always, any session)
        for sym in list(positions.keys()):
            bars=get_bars(PAIRS[sym])
            if not bars: continue
            price=bars[-1]["c"]; pos=positions[sym]
            bars_held=pos.get("bars",0)+1; positions[sym]["bars"]=bars_held
            hit_stop= price<=pos["stop"] if pos["side"]=="long" else price>=pos["stop"]
            hit_tgt=  price>=pos["target"] if pos["side"]=="long" else price<=pos["target"]
            if hit_stop:        close_trade(sym,"STOP",price)
            elif hit_tgt:       close_trade(sym,"TARGET",price)
            elif bars_held>=48: close_trade(sym,"TIMEOUT",price)
            time.sleep(0.3)

        # New entries — only during active sessions
        if session and len(positions)<MAX_OPEN:
            active_pairs = SESSIONS[session]
            for sym in active_pairs:
                if sym in positions: continue
                if len(positions)>=MAX_OPEN: break
                bars=get_bars(PAIRS[sym])
                if not bars or len(bars)<MIN_BARS: continue
                signal,atr,price=get_signal(bars, session)
                if not signal: continue
                risk_usd=equity*RISK_PCT
                stop_d=atr*ATR_MULT
                stop_p=(price-stop_d) if signal=="long" else (price+stop_d)
                tgt_p= (price+stop_d*RR) if signal=="long" else (price-stop_d*RR)
                positions[sym]={"entry":price,"side":signal,"stop":stop_p,"target":tgt_p,
                                "risk_usd":risk_usd,"atr":atr,"bars":0}
                total_p=(equity-ACCOUNT)/ACCOUNT*100
                log(f"📈 {session.upper()} {sym} {signal.upper()} @ {price:.5f} | stop={stop_p:.5f} tgt={tgt_p:.5f} risk=${risk_usd:.0f} | total={total_p:+.2f}%")
                time.sleep(0.3)
        elif not session:
            log(f"No active session — holding. Open positions: {len(positions)}")

        time.sleep(SCAN_SEC)

if __name__=="__main__":
    run()
