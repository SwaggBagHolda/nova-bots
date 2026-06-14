#!/usr/bin/env python3
"""
Nova Trend Rider — Pure Trend Following Bot
Gets in on a confirmed breakout, then RIDES the trend with a trailing stop.
No fixed target. Stays in until the trend dies.

Entry:
  - ADX > 25 (trending market confirmed)
  - Price breaks 20-bar high (LONG) or 20-bar low (SHORT)
  - EMA 21 > EMA 50 for longs | EMA 21 < EMA 50 for shorts
  - RSI 50-70 for longs | RSI 30-50 for shorts
  - Volume 1.3x average (conviction)
  - Candle must CLOSE above/below breakout level (no wicks)

Trail Stop Logic:
  - Initial stop: 1.5x ATR below entry
  - After 1R gain: trail at 1.2x ATR from highest close
  - After 2R gain: tighten trail to 0.8x ATR from highest close
  - After 3R gain: trail at 0.5x ATR (lock in most of the move)
  - Stop only moves UP (longs) or DOWN (shorts) — never backwards
  - No fixed target — ride until trail stop hit or 120-bar timeout (10hrs)

Assets: BTC/USD, ETH/USD, AVAX/USD, ADA/USD, EUR/USD, GBP/USD
Max: 4 concurrent positions
Risk: $600/trade, max $10,000 position
"""

import requests, json, math, time
from datetime import datetime, timezone

KEY  = "PKHFMGMEDX45XRMPT4OWYIKKR4"
SEC  = "DqVA62jPiJuF88VmnusrDAQxBdsgnDahvQWuXiUcdt62"
BASE = "https://paper-api.alpaca.markets"
DATA = "https://data.alpaca.markets"
H    = {"APCA-API-KEY-ID": KEY, "APCA-API-SECRET-KEY": SEC, "Content-Type": "application/json"}
DH   = {"APCA-API-KEY-ID": KEY, "APCA-API-SECRET-KEY": SEC}

ASSETS    = ["BTC/USD", "ETH/USD", "AVAX/USD", "ADA/USD", "EUR/USD", "GBP/USD"]
POS_FILE  = "/tmp/trend_rider_positions.json"
LOG_FILE  = "/tmp/trend_rider.log"
COOL_FILE = "/tmp/trend_rider_cooldown.json"

RISK_USD    = 600
MAX_POS_USD = 10000
MAX_POS     = 4
BREAKOUT_LB = 20     # N-bar lookback for high/low
INIT_TRAIL  = 1.5    # initial stop ATR mult
MIN_ADX     = 25
VOL_MULT    = 1.3
TIMEOUT     = 120    # bars (10 hours)

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

def get_bars(sym, n=120):
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

def atr_val(bars, p=14):
    if len(bars) < 2: return 0.001
    trs = [max(bars[i]['h']-bars[i]['l'],
               abs(bars[i]['h']-bars[i-1]['c']),
               abs(bars[i]['l']-bars[i-1]['c']))
           for i in range(1, len(bars))]
    return sum(trs[-p:]) / min(len(trs), p)

def adx_val(bars, p=14):
    if len(bars) < p*2: return 0
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

def get_trail_mult(r_multiple):
    """Tighten trail as profit grows"""
    if r_multiple >= 3.0: return 0.5
    if r_multiple >= 2.0: return 0.8
    if r_multiple >= 1.0: return 1.2
    return INIT_TRAIL

def get_live_positions():
    try:
        r = requests.get(f"{BASE}/v2/positions", headers=H, timeout=10)
        return {p['symbol']: p for p in r.json()}
    except: return {}

def place_order(sym, side, qty):
    clean = sym.replace("/", "")
    body = {"symbol": clean, "qty": str(round(qty, 6)),
            "side": side, "type": "market", "time_in_force": "gtc"}
    r = requests.post(f"{BASE}/v2/orders", headers=H, json=body, timeout=10)
    return r.json()

def close_pos(sym):
    r = requests.delete(f"{BASE}/v2/positions/{sym.replace('/','')}",
                        headers=H, timeout=10)
    return r.status_code

def run():
    log("=== Nova Trend Rider STARTING ===")
    positions = load_json(POS_FILE, {})
    cooldowns = load_json(COOL_FILE, {})
    now_ts    = time.time()
    live      = get_live_positions()

    log(f"Open: {len(positions)}/{MAX_POS} | Live: {len(live)}")

    # ── MANAGE OPEN POSITIONS ──────────────────────────────────────────
    to_close = []
    for sym, pos in list(positions.items()):
        clean = sym.replace("/","")
        if clean not in live:
            log(f"{sym}: closed externally, removing")
            to_close.append(sym); continue

        bars   = get_bars(sym, 120)
        if not bars: continue
        closes = [b['c'] for b in bars]
        curr   = closes[-1]
        entry  = pos['entry']
        side   = pos['side']
        stop   = pos['stop']
        bars_held = pos.get('bars_held', 0) + 1
        positions[sym]['bars_held'] = bars_held

        a_val      = atr_val(bars)
        init_stop_d = pos.get('init_stop_dist', a_val * INIT_TRAIL)
        r_multiple  = ((curr - entry) / init_stop_d) if side == 'long' else ((entry - curr) / init_stop_d)

        # Update best price seen
        best_price = pos.get('best_price', entry)
        if side == 'long'  and curr > best_price: best_price = curr
        if side == 'short' and curr < best_price: best_price = curr
        positions[sym]['best_price'] = best_price

        # Dynamic trail tightening
        trail_mult = get_trail_mult(r_multiple)
        trail_dist = a_val * trail_mult

        # Trail stop — only moves in profit direction
        if side == 'long':
            new_stop = best_price - trail_dist
            if new_stop > stop:
                stop = new_stop
                positions[sym]['stop'] = stop
        else:
            new_stop = best_price + trail_dist
            if new_stop < stop:
                stop = new_stop
                positions[sym]['stop'] = stop

        pnl_pct = ((curr-entry)/entry*100) if side=='long' else ((entry-curr)/entry*100)

        # Check exit
        reason = None
        if side == 'long'  and curr <= stop: reason = "TRAIL_STOP"
        if side == 'short' and curr >= stop: reason = "TRAIL_STOP"
        if bars_held >= TIMEOUT: reason = "TIMEOUT"

        trail_r_str = f"R={r_multiple:.1f} trail={trail_mult}x"
        if reason:
            close_pos(sym)
            log(f"CLOSED {sym} {side.upper()} | {reason} | PnL: {pnl_pct:+.3f}% | {trail_r_str} | {bars_held} bars")
            if reason == "TRAIL_STOP" and pnl_pct < 0:
                cooldowns[sym] = now_ts + 3600  # 1hr cooldown on losing stop
                save_json(COOL_FILE, cooldowns)
            to_close.append(sym)
        else:
            log(f"RIDING {sym} {side.upper()} | {trail_r_str} | PnL: {pnl_pct:+.3f}% | stop={stop:.5f} | bar {bars_held}/{TIMEOUT}")

    for sym in to_close: positions.pop(sym, None)
    save_json(POS_FILE, positions)

    if len(positions) >= MAX_POS:
        log(f"Max positions ({MAX_POS}), skipping scan")
        return

    # ── SCAN FOR TREND ENTRIES ─────────────────────────────────────────
    for sym in ASSETS:
        if sym in positions: continue
        if cooldowns.get(sym, 0) > now_ts:
            log(f"{sym}: cooldown, skip"); continue
        if len(positions) >= MAX_POS: break

        bars = get_bars(sym, 120)
        if not bars or len(bars) < BREAKOUT_LB + 10: continue

        closes = [b['c'] for b in bars]
        curr   = closes[-1]
        prev   = closes[-2]

        # Indicators
        d_val  = adx_val(bars)
        r_val  = rsi(closes)
        a_val  = atr_val(bars)
        e21    = ema(closes[-40:], 21)
        e50    = ema(closes[-70:], 50)

        # N-bar breakout levels (candle-close based)
        lookback_closes = closes[-(BREAKOUT_LB+1):-1]
        n_high = max(lookback_closes)
        n_low  = min(lookback_closes)

        # Volume conviction
        avg_vol = sum(b['v'] for b in bars[-21:-1]) / 20
        vol_ok  = bars[-1]['v'] >= avg_vol * VOL_MULT

        signal = None

        # LONG: breakout up + trend aligned + ADX trending
        if (prev <= n_high
            and curr > n_high          # candle closes above N-bar high
            and d_val > MIN_ADX        # market is trending
            and e21 > e50              # EMA alignment = uptrend
            and 50 <= r_val <= 72      # momentum building, not overextended
            and vol_ok):               # volume confirms conviction
            signal = 'long'

        # SHORT: breakout down + trend aligned + ADX trending
        elif (prev >= n_low
              and curr < n_low
              and d_val > MIN_ADX
              and e21 < e50            # EMA alignment = downtrend
              and 28 <= r_val <= 50
              and vol_ok):
            signal = 'short'

        if not signal:
            log(f"{sym}: no entry | ADX={d_val:.1f} RSI={r_val:.1f} "
                f"EMA21={'>' if e21>e50 else '<'}EMA50 vol={'✓' if vol_ok else '✗'} "
                f"price={curr:.4f} N-H={n_high:.4f} N-L={n_low:.4f}")
            continue

        # Position sizing — risk $RISK_USD on initial stop
        init_stop_dist = a_val * INIT_TRAIL
        stop_p = (curr - init_stop_dist) if signal == 'long' else (curr + init_stop_dist)
        qty    = min(RISK_USD / init_stop_dist, MAX_POS_USD / curr)
        qty    = max(qty, 0.0001)

        order = place_order(sym, signal, qty)
        if order.get('id'):
            positions[sym] = {
                'entry':          curr,
                'side':           signal,
                'stop':           stop_p,
                'best_price':     curr,
                'qty':            qty,
                'bars_held':      0,
                'init_stop_dist': init_stop_dist,
                'breakout_level': n_high if signal=='long' else n_low
            }
            save_json(POS_FILE, positions)
            log(f"🚀 TREND ENTRY {sym} {signal.upper()} | entry={curr:.4f} "
                f"init_stop={stop_p:.4f} ({INIT_TRAIL}x ATR) | "
                f"ADX={d_val:.1f} RSI={r_val:.1f} EMA21={'>' if e21>e50 else '<'}EMA50 | "
                f"Riding until trail hit or {TIMEOUT} bars")
        else:
            log(f"{sym}: order failed | {order.get('message','?')}")

    save_json(POS_FILE, positions)
    log(f"Done. Trend positions: {len(positions)}/{MAX_POS}")

if __name__ == "__main__":
    run()
