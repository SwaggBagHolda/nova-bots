#!/usr/bin/env python3
"""
Nova Forex Scalper — EUR/USD & GBP/USD
Strategy: London/NY Session Momentum + BB Mean Reversion
- London session: 3am-12pm ET → momentum breakouts
- NY overlap 8am-12pm ET → highest priority
- Asian session: mean reversion only
Indicators: EMA 9/21, BB(20,2), RSI(14), ATR(14), ADX(14)
Logic:
  TREND (ADX > 25): EMA9 cross EMA21 + RSI aligned + price above/below VWAP
  RANGE (ADX < 25): BB mean reversion (same as chop bot)
Exit: 1.5x ATR target | 0.8x ATR stop | 24-bar timeout | candle-close
Uses Alpaca forex (paper) — EUR/USD, GBP/USD
"""

import requests, json, math, time, os
from datetime import datetime, timezone

KEY = "PKHFMGMEDX45XRMPT4OWYIKKR4"
SEC = "DqVA62jPiJuF88VmnusrDAQxBdsgnDahvQWuXiUcdt62"
BASE = "https://paper-api.alpaca.markets"
DATA = "https://data.alpaca.markets"
H    = {"APCA-API-KEY-ID": KEY, "APCA-API-SECRET-KEY": SEC, "Content-Type": "application/json"}
DH   = {"APCA-API-KEY-ID": KEY, "APCA-API-SECRET-KEY": SEC}

ASSETS = ["EUR/USD", "GBP/USD"]
POS_FILE  = "/tmp/forex_positions.json"
LOG_FILE  = "/tmp/forex_scalper.log"
COOL_FILE = "/tmp/forex_cooldown.json"

RISK_USD   = 400
MAX_POS    = 5000
STOP_MULT  = 0.8
TGT_MULT   = 1.5
TIMEOUT    = 24   # bars (5min = 2hrs)
MIN_RR     = 1.5

def log(msg):
    ts = datetime.now().strftime("%m/%d %H:%M")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f: f.write(line + "\n")

def load_json(p, d):
    try:
        with open(p) as f: return json.load(f)
    except: return d

def save_json(p, d):
    with open(p,"w") as f: json.dump(d, f, indent=2)

def get_bars(sym, n=80):
    url = f"{DATA}/v1beta3/crypto/us/bars?symbols={sym}&timeframe=5Min&limit={n}"
    try:
        r = requests.get(url, headers=DH, timeout=10)
        return r.json().get("bars", {}).get(sym, [])
    except: return []

def ema(closes, p):
    if not closes: return 0
    k = 2/(p+1); e = closes[0]
    for c in closes[1:]: e = c*k + e*(1-k)
    return e

def rsi(c, p=14):
    if len(c)<p+1: return 50
    d=[c[i+1]-c[i] for i in range(len(c)-1)]
    g=[max(x,0) for x in d[-p:]]; l=[abs(min(x,0)) for x in d[-p:]]
    ag,al=sum(g)/p,sum(l)/p
    return 100 if al==0 else 100-(100/(1+ag/al))

def bb(c, p=20, s=2.0):
    if len(c)<p: return None,None,None
    w=c[-p:]; m=sum(w)/p
    sd=math.sqrt(sum((x-m)**2 for x in w)/p)
    return m-s*sd, m, m+s*sd

def atr_val(bars, p=14):
    if len(bars)<2: return 0.0001
    trs=[max(bars[i]['h']-bars[i]['l'],abs(bars[i]['h']-bars[i-1]['c']),abs(bars[i]['l']-bars[i-1]['c']))
         for i in range(1,len(bars))]
    return sum(trs[-p:])/min(len(trs),p)

def adx_val(bars, p=14):
    if len(bars)<p*2: return 50
    pd_,md_,tr_=[],[],[]
    for i in range(1,len(bars)):
        h,l,ph,pl,pc=bars[i]['h'],bars[i]['l'],bars[i-1]['h'],bars[i-1]['l'],bars[i-1]['c']
        pd_.append(max(h-ph,0) if (h-ph)>(pl-l) else 0)
        md_.append(max(pl-l,0) if (pl-l)>(h-ph) else 0)
        tr_.append(max(h-l,abs(h-pc),abs(l-pc)))
    a=sum(tr_[-p:])/p
    if a==0: return 0
    pi=(sum(pd_[-p:])/p)/a*100; mi=(sum(md_[-p:])/p)/a*100
    return abs(pi-mi)/(pi+mi+1e-9)*100

def vwap(bars):
    pv=sum(b['c']*b['v'] for b in bars[-50:])
    v=sum(b['v'] for b in bars[-50:])
    return pv/v if v>0 else bars[-1]['c']

def get_positions():
    try:
        r=requests.get(f"{BASE}/v2/positions",headers=H,timeout=10)
        return {p['symbol']:p for p in r.json()}
    except: return {}

def place_order(sym, side, qty):
    clean=sym.replace("/","")
    body={"symbol":clean,"qty":str(round(qty,4)),"side":side,"type":"market","time_in_force":"gtc"}
    r=requests.post(f"{BASE}/v2/orders",headers=H,json=body,timeout=10)
    return r.json()

def close_pos(sym):
    clean=sym.replace("/","")
    r=requests.delete(f"{BASE}/v2/positions/{clean}",headers=H,timeout=10)
    return r.status_code

def get_session():
    hour=datetime.now(timezone.utc).hour
    if 8<=hour<12: return "NY_OVERLAP"   # Best — high volume
    if 3<=hour<8:  return "LONDON"       # Good — trending
    if 12<=hour<21: return "NY"          # Good — trending
    return "ASIA"                         # Range only

def run():
    log("=== Nova Forex Scalper STARTING ===")
    positions = load_json(POS_FILE, {})
    cooldowns = load_json(COOL_FILE, {})
    now_ts = time.time()
    session = get_session()
    log(f"Session: {session}")

    live = get_positions()

    # MANAGE OPEN POSITIONS
    to_close = []
    for sym, pos in list(positions.items()):
        clean=sym.replace("/","")
        if clean not in live:
            to_close.append(sym); continue

        bars=get_bars(sym,80)
        if not bars: continue
        closes=[b['c'] for b in bars]
        curr=closes[-1]; entry=pos['entry']; side=pos['side']
        stop=pos['stop']; target=pos['target']
        bars_held=pos.get('bars_held',0)+1
        positions[sym]['bars_held']=bars_held
        pnl_pct=((curr-entry)/entry*100) if side=='long' else ((entry-curr)/entry*100)

        reason=None
        if side=='long':
            if curr<=stop: reason="STOP"
            elif curr>=target: reason="TARGET"
        else:
            if curr>=stop: reason="STOP"
            elif curr<=target: reason="TARGET"
        if bars_held>=TIMEOUT: reason="TIMEOUT"

        if reason:
            close_pos(sym)
            log(f"CLOSED {sym} {side.upper()} | {reason} | PnL: {pnl_pct:+.4f}%")
            if reason=="STOP": cooldowns[sym]=now_ts+1800
            save_json(COOL_FILE,cooldowns)
            to_close.append(sym)
        else:
            log(f"HOLD {sym} {side.upper()} | bar {bars_held}/{TIMEOUT} | PnL: {pnl_pct:+.4f}%")

    for sym in to_close: positions.pop(sym,None)
    save_json(POS_FILE,positions)

    if len(positions)>=2:
        log("Max positions (2/2), skipping scan")
        return

    for sym in ASSETS:
        if sym in positions: continue
        if cooldowns.get(sym,0)>now_ts:
            log(f"{sym}: cooldown active"); continue
        if len(positions)>=2: break

        bars=get_bars(sym,80)
        if not bars or len(bars)<30: continue
        closes=[b['c'] for b in bars]
        curr=closes[-1]; prev=closes[-2]
        e9=ema(closes[-30:],9); e21=ema(closes[-40:],21)
        r_val=rsi(closes); a_val=atr_val(bars); d_val=adx_val(bars)
        lb,mid,ub=bb(closes); vw=vwap(bars)
        if lb is None: continue

        signal=None

        if d_val>25 and session in ("NY_OVERLAP","LONDON","NY"):
            # TREND MODE: EMA crossover + RSI + VWAP
            ema_prev9=ema(closes[-31:-1],9); ema_prev21=ema(closes[-41:-1],21)
            bullish_cross = ema_prev9<ema_prev21 and e9>e21
            bearish_cross = ema_prev9>ema_prev21 and e9<e21
            if bullish_cross and r_val>50 and curr>vw: signal='long'
            elif bearish_cross and r_val<50 and curr<vw: signal='short'
        elif d_val<25:
            # RANGE MODE: BB mean reversion
            if prev<=lb and curr>lb and r_val<35: signal='long'
            elif prev>=ub and curr<ub and r_val>65: signal='short'

        if not signal:
            log(f"{sym}: no signal | ADX={d_val:.1f} RSI={r_val:.1f} session={session}")
            continue

        stop_d=a_val*STOP_MULT
        tgt_d=a_val*TGT_MULT
        stop_p=(curr-stop_d) if signal=='long' else (curr+stop_d)
        tgt_p=(curr+tgt_d) if signal=='long' else (curr-tgt_d)
        rr=tgt_d/stop_d
        if rr<MIN_RR:
            log(f"{sym}: RR {rr:.2f} < {MIN_RR}, skip"); continue

        qty=min(RISK_USD/(stop_d/curr*curr)/curr, MAX_POS/curr)
        qty=max(qty,0.01)
        order=place_order(sym,signal,qty)
        if order.get('id'):
            positions[sym]={'entry':curr,'side':signal,'stop':stop_p,'target':tgt_p,'qty':qty,'bars_held':0,'rr':round(rr,2)}
            save_json(POS_FILE,positions)
            log(f"ENTERED {sym} {signal.upper()} | entry={curr:.5f} stop={stop_p:.5f} tgt={tgt_p:.5f} RR={rr:.2f}x | {session}")
        else:
            log(f"{sym}: order failed | {order.get('message','?')}")

    save_json(POS_FILE,positions)
    log(f"Done. Forex positions: {len(positions)}/2")

if __name__=="__main__":
    run()
