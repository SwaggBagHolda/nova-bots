#!/usr/bin/env python3
"""
Nova Universal Scalper v2 — WITH TRAILING STOPS + PYRAMIDING + COMPOUNDING
Covers: Crypto, Forex, Futures
Strategy: BB+RSI Mean Reversion in chop | EMA momentum in trend
Trails on 0.5x ATR gain, pyramids up to 2x, compounds equity at 0.16%
"""

import requests, json, math, time
from datetime import datetime, timezone

KEY  = "PKHFMGMEDX45XRMPT4OWYIKKR4"
SEC  = "DqVA62jPiJuF88VmnusrDAQxBdsgnDahvQWuXiUcdt62"
BASE = "https://paper-api.alpaca.markets"
DATA = "https://data.alpaca.markets"
H    = {"APCA-API-KEY-ID": KEY, "APCA-API-SECRET-KEY": SEC, "Content-Type": "application/json"}
DH   = {"APCA-API-KEY-ID": KEY, "APCA-API-SECRET-KEY": SEC}

# All assets — grouped by category
ASSETS = {
    "crypto": ["ETH/USD", "ADA/USD", "UNI/USD", "AVAX/USD"],
    "forex":  ["EUR/USD", "GBP/USD"],
    "futures":["BTC/USD", "ETH/USD"],
}
ALL_ASSETS = list(dict.fromkeys(
    ["BTC/USD","ETH/USD","ADA/USD","UNI/USD","AVAX/USD","EUR/USD","GBP/USD"]
))

POS_FILE  = "/tmp/univ_scalper_positions.json"
LOG_FILE  = "/tmp/univ_scalper.log"
COOL_FILE = "/tmp/univ_scalper_cooldown.json"

RISK_USD  = 300
MAX_POS_USD = 3000
MAX_TOTAL_POS = 6
STOP_MULT = 0.9
TGT_MULT  = 1.5
TIMEOUT   = 36   # bars (3hrs at 5min)
MIN_RR    = 1.3
ADX_SPLIT = 28   # below = range, above = trend

# ── TRAILING STOPS + PYRAMIDING + COMPOUNDING ────────────────────────────────
TRAIL_AFTER_GAIN = 0.5    # trail after 0.5x ATR profit
TRAIL_DIST = 0.4          # trail by 0.4x ATR
PYRAMID_LEVELS = 2        # up to 2x original size
PYRAMID_GAIN_MULT = 1.5   # pyramid after 1.5x ATR gain
COMPOUND_PCT = 0.16       # risk 0.16% of equity per trade
EQUITY_REFRESH_SECS = 60  # refresh equity every 60 sec

equity_cache = {'value': 250000, 'timestamp': 0}

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
    with open(p, "w") as f: json.dump(d, f, indent=2)

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

def get_bars(sym, n=80):
    url = f"{DATA}/v1beta3/crypto/us/bars?symbols={sym}&timeframe=5Min&limit={n}"
    try:
        r = requests.get(url, headers=DH, timeout=10)
        return r.json().get("bars", {}).get(sym, [])
    except: return []

def ema(closes, p):
    if len(closes) < p: return closes[-1] if closes else 0
    k = 2/(p+1); e = closes[0]
    for c in closes[1:]: e = c*k + e*(1-k)
    return e

def rsi(c, p=14):
    if len(c) < p+1: return 50
    d = [c[i+1]-c[i] for i in range(len(c)-1)]
    g = [max(x,0) for x in d[-p:]]; l = [abs(min(x,0)) for x in d[-p:]]
    ag, al = sum(g)/p, sum(l)/p
    return 100 if al == 0 else 100-(100/(1+ag/al))

def bb(c, p=20, s=2.0):
    if len(c) < p: return None, None, None
    w = c[-p:]; m = sum(w)/p
    sd = math.sqrt(sum((x-m)**2 for x in w)/p)
    return m-s*sd, m, m+s*sd

def atr_val(bars, p=14):
    if len(bars) < 2: return 0.001
    trs = [max(bars[i]['h']-bars[i]['l'],
               abs(bars[i]['h']-bars[i-1]['c']),
               abs(bars[i]['l']-bars[i-1]['c']))
           for i in range(1, len(bars))]
    return sum(trs[-p:])/min(len(trs), p)

def adx_val(bars, p=14):
    if len(bars) < p*2: return 50
    pd_, md_, tr_ = [], [], []
    for i in range(1, len(bars)):
        h,l,ph,pl,pc = bars[i]['h'],bars[i]['l'],bars[i-1]['h'],bars[i-1]['l'],bars[i-1]['c']
        pd_.append(max(h-ph,0) if (h-ph)>(pl-l) else 0)
        md_.append(max(pl-l,0) if (pl-l)>(h-ph) else 0)
        tr_.append(max(h-l, abs(h-pc), abs(l-pc)))
    a = sum(tr_[-p:])/p
    if a == 0: return 0
    pi = (sum(pd_[-p:])/p)/a*100
    mi = (sum(md_[-p:])/p)/a*100
    return abs(pi-mi)/(pi+mi+1e-9)*100

def vwap(bars):
    last = bars[-50:] if len(bars) >= 50 else bars
    pv = sum(b['c']*b['v'] for b in last)
    v = sum(b['v'] for b in last)
    return pv/v if v > 0 else bars[-1]['c']

def get_live_positions():
    try:
        r = requests.get(f"{BASE}/v2/positions", headers=H, timeout=10)
        return {p['symbol']: p for p in r.json()}
    except: return {}

def place_order(sym, side, qty):
    clean = sym.replace("/", "")
    body = {"symbol": clean, "qty": str(round(qty, 6)), "side": side,
            "type": "market", "time_in_force": "gtc"}
    r = requests.post(f"{BASE}/v2/orders", headers=H, json=body, timeout=10)
    return r.json()

def close_pos(sym):
    clean = sym.replace("/", "")
    r = requests.delete(f"{BASE}/v2/positions/{clean}", headers=H, timeout=10)
    return r.status_code

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

def get_signal(bars, closes, sym):
    """Returns (signal, mode, metadata) or (None, None, None)"""
    if len(closes) < 30: return None, None, None
    curr, prev = closes[-1], closes[-2]
    a_val = atr_val(bars)
    d_val = adx_val(bars)
    r_val = rsi(closes)
    lb, mid, ub = bb(closes)
    if lb is None: return None, None, None

    if d_val < ADX_SPLIT:
        # RANGE MODE — BB+RSI mean reversion
        if prev <= lb and curr > lb and r_val < 33 and curr > prev:
            return 'long', 'RANGE', {'adx': d_val, 'rsi': r_val, 'mid': mid}
        if prev >= ub and curr < ub and r_val > 67 and curr < prev:
            return 'short', 'RANGE', {'adx': d_val, 'rsi': r_val, 'mid': mid}
    else:
        # TREND MODE — EMA cross + VWAP
        e9  = ema(closes[-30:], 9)
        e21 = ema(closes[-40:], 21)
        e9p = ema(closes[-31:-1], 9)
        e21p= ema(closes[-41:-1], 21)
        vw  = vwap(bars)
        bull_cross = e9p < e21p and e9 > e21
        bear_cross = e9p > e21p and e9 < e21
        if bull_cross and r_val > 48 and curr > vw:
            return 'long', 'TREND', {'adx': d_val, 'rsi': r_val, 'e9': e9, 'e21': e21}
        if bear_cross and r_val < 52 and curr < vw:
            return 'short', 'TREND', {'adx': d_val, 'rsi': r_val, 'e9': e9, 'e21': e21}

    return None, None, None

def run():
    log("=== Nova Universal Scalper v2 [TRAILING PYRAMID COMPOUND] ===")
    positions = load_json(POS_FILE, {})
    cooldowns = load_json(COOL_FILE, {})
    now_ts = time.time()
    live = get_live_positions()
    equity = get_equity()

    log(f"Open: {len(positions)}/{MAX_TOTAL_POS} | Live broker: {len(live)} | Equity: ${equity:,.2f}")

    # --- MANAGE OPEN POSITIONS ---
    to_close = []
    for sym, pos in list(positions.items()):
        clean = sym.replace("/","")
        if clean not in live:
            log(f"{sym}: gone externally, removing")
            to_close.append(sym); continue

        bars = get_bars(sym, 80)
        if not bars: continue
        closes = [b['c'] for b in bars]
        curr = closes[-1]
        entry, side = pos['entry'], pos['side']
        stop, target = pos['stop'], pos['target']
        bars_held = pos.get('bars_held', 0) + 1
        positions[sym]['bars_held'] = bars_held
        pnl_pct = ((curr-entry)/entry*100) if side=='long' else ((entry-curr)/entry*100)
        mode = pos.get('mode','?')
        
        a_val = atr_val(bars)
        
        # ── UPDATE TRAILING STOP ──────────────────────────────────
        if update_trailing_stop(pos, curr, a_val):
            log(f"{sym}: trailing stop moved to {pos['stop']:.4f}")
        
        # ── ATTEMPT PYRAMID ───────────────────────────────────────
        if try_pyramid(positions, sym, pos, curr, a_val, bars_held):
            log(f"{sym}: pyramided to level {pos['pyramid_level']} | new qty={pos['qty']:.4f}")

        stop = pos['stop']
        reason = None
        if side == 'long':
            if curr <= stop:  reason = "STOP"
            elif curr >= target: reason = "TARGET"
        else:
            if curr >= stop:  reason = "STOP"
            elif curr <= target: reason = "TARGET"
        if bars_held >= TIMEOUT: reason = "TIMEOUT"

        if reason:
            close_pos(sym)
            log(f"CLOSED {sym} [{mode}] {side.upper()} | {reason} | PnL: {pnl_pct:+.3f}%")
            if reason == "STOP":
                cooldowns[sym] = now_ts + 1800
                save_json(COOL_FILE, cooldowns)
            to_close.append(sym)
        else:
            log(f"HOLD {sym} [{mode}] {side.upper()} | bar {bars_held}/{TIMEOUT} | PnL: {pnl_pct:+.3f}%")

    for sym in to_close: positions.pop(sym, None)
    save_json(POS_FILE, positions)

    if len(positions) >= MAX_TOTAL_POS:
        log(f"Max positions ({MAX_TOTAL_POS}) reached, skipping scan")
        return

    # --- SCAN ALL ASSETS ---
    for sym in ALL_ASSETS:
        if sym in positions: continue
        if cooldowns.get(sym, 0) > now_ts:
            log(f"{sym}: cooldown active, skip"); continue
        if len(positions) >= MAX_TOTAL_POS: break

        bars = get_bars(sym, 80)
        if not bars or len(bars) < 30: continue
        closes = [b['c'] for b in bars]
        curr = closes[-1]

        signal, mode, meta = get_signal(bars, closes, sym)
        if not signal:
            d = adx_val(bars); r = rsi(closes)
            log(f"{sym}: no signal | ADX={d:.1f} RSI={r:.1f} mode={'RANGE' if d<ADX_SPLIT else 'TREND'}")
            continue

        a_val = atr_val(bars)
        stop_d = a_val * STOP_MULT
        tgt_d  = a_val * TGT_MULT
        stop_p = (curr - stop_d) if signal == 'long' else (curr + stop_d)
        tgt_p  = (curr + tgt_d)  if signal == 'long' else (curr - tgt_d)

        # Use BB midline as target in range mode (tighter, higher WR)
        if mode == 'RANGE' and meta.get('mid'):
            mid_dist = abs(meta['mid'] - curr)
            if mid_dist > stop_d * MIN_RR:
                tgt_p = meta['mid']
                tgt_d = mid_dist

        rr = tgt_d / stop_d if stop_d > 0 else 0
        if rr < MIN_RR:
            log(f"{sym}: RR {rr:.2f} too low, skip"); continue

        # ── COMPOUND SIZING (0.16% risk per equity) ────────────────
        risk_this_trade = equity * (COMPOUND_PCT / 100)
        qty = min(risk_this_trade / (stop_d / curr * curr) / curr, MAX_POS_USD / curr)
        qty = max(qty, 0.0001)

        order = place_order(sym, signal, qty)
        if order.get('id'):
            positions[sym] = {
                'entry': curr, 'side': signal, 'stop': stop_p,
                'target': tgt_p, 'qty': qty, 'bars_held': 0,
                'rr': round(rr, 2), 'mode': mode, 'pyramid_level': 1
            }
            save_json(POS_FILE, positions)
            log(f"ENTERED {sym} [{mode}] {signal.upper()} | entry={curr:.5f} stop={stop_p:.5f} tgt={tgt_p:.5f} RR={rr:.2f}x")
        else:
            log(f"{sym}: order failed | {order.get('message','?')}")

    save_json(POS_FILE, positions)
    log(f"Done. Positions: {len(positions)}/{MAX_TOTAL_POS}")

if __name__ == "__main__":
    run()
