#!/usr/bin/env python3
"""
Nova Breakout Trader v4 — WITH TRAILING STOPS + PYRAMIDING + COMPOUNDING
Research-optimized: lookback=25, ADX>=30, stop=1.0x ATR, target=2.5R
Trails on 0.5x ATR gain, pyramids up to 2x, compounds equity at 0.16%
"""
import requests, json, math, time, sys, importlib.util
from datetime import datetime, timezone

KEY  = "PKHFMGMEDX45XRMPT4OWYIKKR4"
SEC  = "DqVA62jPiJuF88VmnusrDAQxBdsgnDahvQWuXiUcdt62"
BASE = "https://paper-api.alpaca.markets"
DATA = "https://data.alpaca.markets"
H    = {"APCA-API-KEY-ID":KEY,"APCA-API-SECRET-KEY":SEC,"Content-Type":"application/json"}
DH   = {"APCA-API-KEY-ID":KEY,"APCA-API-SECRET-KEY":SEC}

# ── RESEARCH-PROVEN PARAMS ──────────────────────────────────────────────────
ASSETS      = ["BTC/USD","ETH/USD","ADA/USD","AVAX/USD","UNI/USD","EUR/USD","GBP/USD"]
LOOKBACK    = 25       # research: 25-bar high/low
ADX_MIN     = 30       # research: ADX>=30 confirms real trend
STOP_MULT   = 1.0      # research: 1.0x ATR
TGT_MULT    = 2.5      # research: 2.5R target
VOL_MULT    = 1.3      # volume confirmation
TIMEOUT     = 60       # bars (5 hours)
RISK_USD    = 500
MAX_POS_USD = 8000
MAX_POS     = 4
POS_FILE    = "/tmp/breakout_positions.json"
COOL_FILE   = "/tmp/breakout_cooldown.json"
LOG_FILE    = "/tmp/breakout_trader.log"
BOT_NAME    = "breakout_trader_v4"

# ── TRAILING STOPS + PYRAMIDING + COMPOUNDING ────────────────────────────────
TRAIL_AFTER_GAIN = 0.5    # trail after 0.5x ATR profit
TRAIL_DIST = 0.4          # trail by 0.4x ATR
PYRAMID_LEVELS = 2        # up to 2x original size
PYRAMID_GAIN_MULT = 1.5   # pyramid after 1.5x ATR gain
COMPOUND_PCT = 0.16       # risk 0.16% of equity per trade
EQUITY_REFRESH_SECS = 60  # refresh equity every 60 sec

equity_cache = {'value': 250000, 'timestamp': 0}

def log(msg):
    ts=datetime.now().strftime("%m/%d %H:%M")
    line=f"[{ts}] {msg}"; print(line)
    with open(LOG_FILE,"a") as f: f.write(line+"\n")

def load_json(p,d):
    try:
        with open(p) as f: return json.load(f)
    except: return d

def save_json(p,d):
    with open(p,"w") as f: json.dump(d,f,indent=2)

def get_equity():
    """Fetch live account equity from Alpaca API."""
    global equity_cache
    now = time.time()
    if now - equity_cache['timestamp'] < EQUITY_REFRESH_SECS:
        return equity_cache['value']
    try:
        r = requests.get(f"{BASE}/v2/account", headers=H, timeout=10)
        data = r.json()
        eq = float(data.get('equity', 250000))
        equity_cache = {'value': eq, 'timestamp': now}
        return eq
    except:
        return equity_cache['value']

def get_bars(sym,n=120):
    url=f"{DATA}/v1beta3/crypto/us/bars?symbols={sym}&timeframe=5Min&limit={n}"
    try:
        r=requests.get(url,headers=DH,timeout=10)
        return r.json().get("bars",{}).get(sym,[])
    except: return []

def ema(closes,p):
    if len(closes)<p: return closes[-1] if closes else 0
    k=2/(p+1); e=closes[0]
    for c in closes[1:]: e=c*k+e*(1-k)
    return e

def rsi(c,p=14):
    if len(c)<p+1: return 50
    d=[c[i+1]-c[i] for i in range(len(c)-1)]
    g=[max(x,0) for x in d[-p:]]; l=[abs(min(x,0)) for x in d[-p:]]
    ag,al=sum(g)/p,sum(l)/p
    return 100 if al==0 else 100-(100/(1+ag/al))

def atr_val(bars,p=14):
    if len(bars)<2: return 0.001
    trs=[max(bars[i]['h']-bars[i]['l'],abs(bars[i]['h']-bars[i-1]['c']),abs(bars[i]['l']-bars[i-1]['c']))
         for i in range(1,len(bars))]
    return sum(trs[-p:])/min(len(trs),p)

def adx_val(bars,p=14):
    if len(bars)<p*2: return 0
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

def learn_after_trade(sym,side,entry,exit_p,stop_dist,reason,bars_held,indicators):
    try:
        # ── POST TO BASE44 DB (persistent) ───────────────────────────────
        try:
            from nova_db_logger import post_trade as _db_post
            pnl_pct = ((exit_p - entry) / entry * 100) if side == 'long' else ((entry - exit_p) / entry * 100)
            pnl_usd = pnl_pct * 10  # approximate
            status = "WIN" if pnl_usd > 0 else "LOSS"
            import threading
            threading.Thread(target=_db_post, args=(BOT_NAME, sym, side.upper(), entry, exit_p, pnl_usd, pnl_pct, reason, status), daemon=True).start()
        except Exception as dbe: print(f"[DB] {dbe}")
        # ─────────────────────────────────────────────────────────────────
        sys.path.insert(0,'/root')
        spec=importlib.util.spec_from_file_location("tl","/tmp/nova_trade_logger.py")
        tl=importlib.util.module_from_spec(spec); spec.loader.exec_module(tl)
        tl.log_trade(BOT_NAME,sym,side,"BREAKOUT",entry,exit_p,stop_dist,reason,bars_held,indicators)
        spec2=importlib.util.spec_from_file_location("le","/tmp/nova_learning_engine.py")
        le=importlib.util.module_from_spec(spec2); spec2.loader.exec_module(le)
        le.run(trigger="trade_close",bot_name=BOT_NAME)
    except: pass

def get_live_positions():
    try:
        r=requests.get(f"{BASE}/v2/positions",headers=H,timeout=10)
        return {p['symbol']:p for p in r.json()}
    except: return {}

def place_order(sym,side,qty):
    clean=sym.replace("/","")
    body={"symbol":clean,"qty":str(round(qty,6)),"side":side,"type":"market","time_in_force":"gtc"}
    r=requests.post(f"{BASE}/v2/orders",headers=H,json=body,timeout=10)
    return r.json()

def close_position(sym):
    requests.delete(f"{BASE}/v2/positions/{sym.replace('/','')}", headers=H, timeout=10)

def update_trailing_stop(pos, curr, a_val):
    """Update trailing stop if price moved favorably beyond trail trigger."""
    entry = pos['entry']
    side = pos['side']
    trail_gain = a_val * TRAIL_AFTER_GAIN
    trail_dist = a_val * TRAIL_DIST
    
    if side == 'long':
        gain = curr - entry
        if gain >= trail_gain:
            new_stop = curr - trail_dist
            if new_stop > pos['stop']:
                pos['stop'] = new_stop
                return True
    else:
        gain = entry - curr
        if gain >= trail_gain:
            new_stop = curr + trail_dist
            if new_stop < pos['stop']:
                pos['stop'] = new_stop
                return True
    return False

def try_pyramid(positions, sym, pos, curr, a_val, bars_held):
    """Attempt to pyramid into winning position."""
    entry = pos['entry']
    side = pos['side']
    pyramid_gain = a_val * PYRAMID_GAIN_MULT
    current_level = pos.get('pyramid_level', 1)
    
    if current_level >= PYRAMID_LEVELS:
        return False
    
    if side == 'long':
        gain = curr - entry
    else:
        gain = entry - curr
    
    if gain >= pyramid_gain:
        new_qty = pos['qty'] * (current_level + 1) / current_level
        pos['qty'] = new_qty
        pos['pyramid_level'] = current_level + 1
        return True
    
    return False

def run():
    log(f"=== Breakout Trader v4 [LB={LOOKBACK} ADX>={ADX_MIN} TRAIL PYRAMID COMPOUND] ===")
    positions=load_json(POS_FILE,{}); cooldowns=load_json(COOL_FILE,{})
    now_ts=time.time(); live=get_live_positions()
    equity = get_equity()
    log(f"Open: {len(positions)}/{MAX_POS} | Live: {len(live)} | Equity: ${equity:,.2f}")

    # ── MANAGE OPEN POSITIONS ────────────────────────────────────────
    to_close=[]
    for sym,pos in list(positions.items()):
        clean=sym.replace("/","")
        if clean not in live:
            log(f"{sym}: externally closed"); to_close.append(sym); continue

        bars=get_bars(sym,120)
        if not bars: continue
        closes=[b['c'] for b in bars]
        curr=closes[-1]; entry=pos['entry']; side=pos['side']
        a_val=atr_val(bars)
        stop=pos['stop']; target=pos['target']
        bars_held=pos.get('bars_held',0)+1
        positions[sym]['bars_held']=bars_held

        pnl_pct=((curr-entry)/entry*100) if side=='long' else ((entry-curr)/entry*100)
        
        # ── UPDATE TRAILING STOP ──────────────────────────────────
        if update_trailing_stop(pos, curr, a_val):
            log(f"{sym}: trailing stop moved to {pos['stop']:.4f}")
        
        # ── ATTEMPT PYRAMID ───────────────────────────────────────
        if try_pyramid(positions, sym, pos, curr, a_val, bars_held):
            log(f"{sym}: pyramided to level {pos['pyramid_level']} | new qty={pos['qty']:.4f}")
        
        stop = pos['stop']
        reason=None
        if side=='long':
            if closes[-1]<=stop: reason="STOP"
            elif closes[-1]>=target: reason="TARGET"
        else:
            if closes[-1]>=stop: reason="STOP"
            elif closes[-1]<=target: reason="TARGET"
        if bars_held>=TIMEOUT: reason="TIMEOUT"

        if reason:
            close_position(sym)
            log(f"CLOSED {sym} {side.upper()} | {reason} | PnL: {pnl_pct:+.3f}% | {bars_held} bars")
            learn_after_trade(sym,side,entry,curr,
                              pos.get('stop_dist',a_val*STOP_MULT),reason,bars_held,
                              pos.get('indicators',{}))
            to_close.append(sym)
            if reason=="STOP": cooldowns[sym]=now_ts+1800
        else:
            log(f"RIDING {sym} {side.upper()} | PnL: {pnl_pct:+.3f}% | bar {bars_held}/{TIMEOUT}")

    for sym in to_close: positions.pop(sym,None)
    save_json(POS_FILE,positions); save_json(COOL_FILE,cooldowns)

    if len(positions)>=MAX_POS: return

    # ── SCAN FOR BREAKOUT ENTRIES ────────────────────────────────────
    for sym in ASSETS:
        if sym in positions: continue
        if cooldowns.get(sym,0)>now_ts: log(f"{sym}: cooldown"); continue
        if len(positions)>=MAX_POS: break

        bars=get_bars(sym,120)
        if not bars or len(bars)<LOOKBACK+10: continue
        closes=[b['c'] for b in bars]
        curr=closes[-1]; prev=closes[-2]

        # N-bar breakout levels (candle-close based, not wicks)
        lb_closes=closes[-(LOOKBACK+1):-1]
        n_high=max(lb_closes); n_low=min(lb_closes)
        d_val=adx_val(bars); r_val=rsi(closes); a_val=atr_val(bars)
        e21=ema(closes[-40:],21); e50=ema(closes[-70:],50)
        avg_vol=sum(b['v'] for b in bars[-21:-1])/20
        vol_ok=bars[-1]['v']>=avg_vol*VOL_MULT

        signal=None
        if (prev<=n_high and curr>n_high and d_val>=ADX_MIN
                and 48<=r_val<=75 and e21>e50 and vol_ok):
            signal='long'
        elif (prev>=n_low and curr<n_low and d_val>=ADX_MIN
                and 25<=r_val<=52 and e21<e50 and vol_ok):
            signal='short'

        if not signal:
            log(f"{sym}: no breakout | ADX={d_val:.1f} RSI={r_val:.1f} vol={'✓' if vol_ok else '✗'} N-H={n_high:.4f} N-L={n_low:.4f}")
            continue

        stop_dist=a_val*STOP_MULT
        stop_p=(curr-stop_dist) if signal=='long' else (curr+stop_dist)
        target_p=(curr+stop_dist*TGT_MULT) if signal=='long' else (curr-stop_dist*TGT_MULT)
        
        # ── COMPOUND SIZING (0.16% risk per equity) ────────────────
        risk_this_trade = equity * (COMPOUND_PCT / 100)
        qty=min(risk_this_trade/stop_dist, MAX_POS_USD/curr)
        qty=max(qty,0.0001)

        order=place_order(sym,signal,qty)
        if order.get('id'):
            positions[sym]={
                'entry':curr,'side':signal,'stop':stop_p,'target':target_p,
                'qty':qty,'bars_held':0,'stop_dist':stop_dist,'pyramid_level':1,
                'indicators':{'adx':round(d_val,1),'rsi':round(r_val,1),'vol_ok':vol_ok}
            }
            save_json(POS_FILE,positions)
            log(f"🚀 BREAKOUT {sym} {signal.upper()} @ {curr:.4f} | target={target_p:.4f} (+{TGT_MULT}R) | ADX={d_val:.1f}")
        else:
            log(f"{sym}: order failed | {order.get('message','?')}")

    log(f"Done. Breakout positions: {len(positions)}/{MAX_POS}")

if __name__=="__main__":
    run()
