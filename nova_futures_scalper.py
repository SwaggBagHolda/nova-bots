#!/usr/bin/env python3
"""
Nova Futures Scalper — ES/NQ Intraday Momentum
Based on: "Beat the Market" paper (Zarattini et al.) + Quantitativo improvements
Strategy: Noise-area breakout with VWAP stops
- Define noise area = avg absolute deviation from open over last 90 bars
- Entry: price breaks OUT of noise area with volume confirmation
- Direction: above = long, below = short
- Stop: VWAP reversion (price crosses back through VWAP)
- Target: 2x noise area extension
- Only trades during RTH: 9:30am - 4:00pm ET
- Uses crypto futures as proxy (BTC, ETH) since Alpaca paper = crypto
- NQ proxy: ETH/USD (high beta tech-like)
- ES proxy: BTC/USD (macro correlated)
"""

import requests, json, math, time
from datetime import datetime, timezone, timedelta

KEY = "PKHFMGMEDX45XRMPT4OWYIKKR4"
SEC = "DqVA62jPiJuF88VmnusrDAQxBdsgnDahvQWuXiUcdt62"
BASE = "https://paper-api.alpaca.markets"
DATA = "https://data.alpaca.markets"
H    = {"APCA-API-KEY-ID": KEY, "APCA-API-SECRET-KEY": SEC, "Content-Type": "application/json"}
DH   = {"APCA-API-KEY-ID": KEY, "APCA-API-SECRET-KEY": SEC}

# Futures proxies via crypto (24/7 liquidity)
# Primary: BTC (macro/ES-like), ETH (tech/NQ-like)
ASSETS = ["BTC/USD", "ETH/USD"]
POS_FILE  = "/tmp/futures_positions.json"
LOG_FILE  = "/tmp/futures_scalper.log"
COOL_FILE = "/tmp/futures_cooldown.json"

RISK_USD  = 500
MAX_POS   = 8000
NOISE_LB  = 90   # lookback for noise area
STOP_MULT = 0.8
TGT_MULT  = 2.0
TIMEOUT   = 30   # bars

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

def get_bars(sym, n=120):
    url = f"{DATA}/v1beta3/crypto/us/bars?symbols={sym}&timeframe=5Min&limit={n}"
    try:
        r = requests.get(url, headers=DH, timeout=10)
        return r.json().get("bars", {}).get(sym, [])
    except: return []

def atr_val(bars, p=14):
    if len(bars)<2: return 1
    trs=[max(bars[i]['h']-bars[i]['l'],abs(bars[i]['h']-bars[i-1]['c']),abs(bars[i]['l']-bars[i-1]['c']))
         for i in range(1,len(bars))]
    return sum(trs[-p:])/min(len(trs),p)

def rsi(c, p=14):
    if len(c)<p+1: return 50
    d=[c[i+1]-c[i] for i in range(len(c)-1)]
    g=[max(x,0) for x in d[-p:]]; l=[abs(min(x,0)) for x in d[-p:]]
    ag,al=sum(g)/p,sum(l)/p
    return 100 if al==0 else 100-(100/(1+ag/al))

def vwap(bars):
    last=bars[-50:] if len(bars)>=50 else bars
    pv=sum(b['c']*b['v'] for b in last)
    v=sum(b['v'] for b in last)
    return pv/v if v>0 else bars[-1]['c']

def noise_area(closes, lb=90):
    """Avg absolute deviation from 'open' proxy (first bar in window)"""
    if len(closes)<lb: lb=len(closes)
    window=closes[-lb:]
    ref=window[0]
    devs=[abs(c-ref) for c in window]
    return sum(devs)/len(devs)

def volume_surge(bars, mult=1.3):
    if len(bars)<20: return True
    avg_v=sum(b['v'] for b in bars[-20:])/20
    return bars[-1]['v'] >= avg_v*mult

def is_trading_hours():
    # Crypto trades 24/7 — but we focus on US hours for momentum
    # 9:30 AM - 4:00 PM ET = 13:30-20:00 UTC
    now=datetime.now(timezone.utc)
    h,m=now.hour,now.minute
    start=(h*60+m)>=13*60+30
    end=(h*60+m)<=20*60
    return start and end

def get_positions():
    try:
        r=requests.get(f"{BASE}/v2/positions",headers=H,timeout=10)
        return {p['symbol']:p for p in r.json()}
    except: return {}

def place_order(sym, side, qty):
    clean=sym.replace("/","")
    body={"symbol":clean,"qty":str(round(qty,6)),"side":side,"type":"market","time_in_force":"gtc"}
    r=requests.post(f"{BASE}/v2/orders",headers=H,json=body,timeout=10)
    return r.json()

def close_pos(sym):
    clean=sym.replace("/","")
    r=requests.delete(f"{BASE}/v2/positions/{clean}",headers=H,timeout=10)
    return r.status_code

def run():
    log("=== Nova Futures Scalper STARTING ===")
    positions = load_json(POS_FILE, {})
    cooldowns = load_json(COOL_FILE, {})
    now_ts = time.time()
    trading = is_trading_hours()
    log(f"US Market Hours: {'YES' if trading else 'NO — monitoring only'}")

    live = get_positions()

    # MANAGE OPEN POSITIONS (always, not just during hours)
    to_close = []
    for sym, pos in list(positions.items()):
        clean=sym.replace("/","")
        if clean not in live:
            to_close.append(sym); continue

        bars=get_bars(sym,120)
        if not bars: continue
        closes=[b['c'] for b in bars]
        curr=closes[-1]; entry=pos['entry']; side=pos['side']
        stop=pos['stop']; target=pos['target']
        bars_held=pos.get('bars_held',0)+1
        positions[sym]['bars_held']=bars_held
        pnl_pct=((curr-entry)/entry*100) if side=='long' else ((entry-curr)/entry*100)

        vw=vwap(bars)
        reason=None
        # VWAP stop (paper says close when price re-enters noise area / crosses VWAP)
        if side=='long':
            if curr<=stop: reason="STOP"
            elif curr>=target: reason="TARGET"
            elif curr<vw and bars_held>3: reason="VWAP_CROSS"
        else:
            if curr>=stop: reason="STOP"
            elif curr<=target: reason="TARGET"
            elif curr>vw and bars_held>3: reason="VWAP_CROSS"
        if bars_held>=TIMEOUT: reason="TIMEOUT"

        if reason:
            close_pos(sym)
            log(f"CLOSED {sym} {side.upper()} | {reason} | PnL: {pnl_pct:+.3f}%")
            if reason=="STOP": cooldowns[sym]=now_ts+1800
            save_json(COOL_FILE,cooldowns)
            to_close.append(sym)
        else:
            log(f"HOLD {sym} {side.upper()} | bar {bars_held}/{TIMEOUT} | PnL: {pnl_pct:+.3f}%")

    for sym in to_close: positions.pop(sym,None)
    save_json(POS_FILE,positions)

    if not trading:
        log("Outside US hours — skipping new entries")
        save_json(POS_FILE,positions); return

    if len(positions)>=2:
        log("Max positions (2/2), skipping scan")
        return

    for sym in ASSETS:
        if sym in positions: continue
        if cooldowns.get(sym,0)>now_ts:
            log(f"{sym}: cooldown active"); continue
        if len(positions)>=2: break

        bars=get_bars(sym,120)
        if not bars or len(bars)<NOISE_LB: continue
        closes=[b['c'] for b in bars]
        curr=closes[-1]
        noise=noise_area(closes, NOISE_LB)
        ref=closes[-NOISE_LB]   # reference "open" for window
        vw=vwap(bars)
        a_val=atr_val(bars)
        r_val=rsi(closes)
        vol_ok=volume_surge(bars, 1.2)

        # Noise breakout: price moved more than noise area from reference
        deviation=curr-ref
        upper_break = deviation > noise and curr>vw and r_val>52 and vol_ok
        lower_break = deviation < -noise and curr<vw and r_val<48 and vol_ok

        signal=None
        if upper_break: signal='long'
        elif lower_break: signal='short'

        if not signal:
            log(f"{sym}: no signal | noise={noise:.2f} dev={deviation:.2f} RSI={r_val:.1f} vol={'✓' if vol_ok else '✗'}")
            continue

        stop_d=a_val*STOP_MULT
        tgt_d=noise*TGT_MULT
        stop_p=(curr-stop_d) if signal=='long' else (curr+stop_d)
        tgt_p=(curr+tgt_d) if signal=='long' else (curr-tgt_d)
        rr=tgt_d/stop_d
        if rr<1.5:
            log(f"{sym}: RR {rr:.2f} too low, skip"); continue

        qty=min(RISK_USD/(stop_d/curr*curr)/curr, MAX_POS/curr)
        qty=max(qty,0.0001)
        order=place_order(sym,signal,qty)
        if order.get('id'):
            positions[sym]={'entry':curr,'side':signal,'stop':stop_p,'target':tgt_p,'qty':qty,'bars_held':0,'rr':round(rr,2),'noise':noise}
            save_json(POS_FILE,positions)
            log(f"ENTERED {sym} {signal.upper()} | entry={curr:.2f} noise={noise:.2f} dev={deviation:.2f} RR={rr:.2f}x")
        else:
            log(f"{sym}: order failed | {order.get('message','?')}")

    save_json(POS_FILE,positions)
    log(f"Done. Futures positions: {len(positions)}/2")

if __name__=="__main__":
    run()
