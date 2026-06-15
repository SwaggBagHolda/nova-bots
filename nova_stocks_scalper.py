import nova_logger
#!/usr/bin/env python3
"""
Nova Stocks Scalper — SPY/QQQ/NVDA/AMZN
Strategy: Opening Range Breakout (ORB) + VWAP Momentum
POWER HOURS ONLY:
  - 9:25–11:30 AM ET (prime session open + ORB window)
  - 3:00–4:00 PM ET  (late power hour, close momentum)
Sleeps during dead zone 11:30 AM – 3:00 PM and outside market hours.
"""

import requests, json, math, time, os
from datetime import datetime, timezone, timedelta

KEY  = os.environ.get("ALPACA_API_KEY",  "PKHFMGMEDX45XRMPT4OWYIKKR4")
SEC  = os.environ.get("ALPACA_SECRET",   "DqVA62jPiJuF88VmnusrDAQxBdsgnDahvQWuXiUcdt62")
BASE = "https://paper-api.alpaca.markets"
DATA = "https://data.alpaca.markets"
H    = {"APCA-API-KEY-ID": KEY, "APCA-API-SECRET-KEY": SEC, "Content-Type": "application/json"}
DH   = {"APCA-API-KEY-ID": KEY, "APCA-API-SECRET-KEY": SEC}

ASSETS   = ["SPY", "QQQ", "NVDA", "AMZN"]
RISK_USD = 500
MAX_POS  = 6000
STOP_M   = 0.8
TGT_M    = 1.8
TIMEOUT  = 20
MIN_RR   = 1.8
VOL_M    = 1.5
SCAN_SEC = 120   # scan every 2 minutes during power hours

# Base44 DB logging
B44_TOKEN = os.environ.get("BASE44_SERVICE_TOKEN", "")
B44_URL   = "https://app.base44.com/api/apps/69bf82ce8c526c379bdab3ce/entities/ScalperTrade"

def log(msg):
    ts = datetime.now().strftime("%m/%d %H:%M")
    line = f"[STOCKS {ts}] {msg}"
    print(line, flush=True)

def log_trade_db(sym, signal, entry, exit_p, pnl_usd, pnl_pct, status, reason):
    if not B44_TOKEN: return
    try:
        payload = {
            "bot_name": "Stocks Scalper",
            "symbol": sym,
            "scan_time": datetime.now(timezone.utc).isoformat(),
            "signal": signal.upper(),
            "entry_price": round(entry, 4),
            "exit_price": round(exit_p, 4),
            "pnl_usd": round(pnl_usd, 2),
            "pnl_pct": round(pnl_pct, 4),
            "trade_status": status,
            "reason": reason
        }
        requests.post(B44_URL, json=payload,
                      headers={"Authorization": f"Bearer {B44_TOKEN}",
                               "Content-Type": "application/json"}, timeout=8)
    except: pass

def is_power_hour():
    """True during 9:25-11:30 AM ET or 3:00-4:00 PM ET"""
    now = datetime.now(timezone.utc)
    mins = now.hour * 60 + now.minute
    # UTC offsets: ET = UTC-4 (EDT summer)
    # 9:25 ET  = 13:25 UTC | 11:30 ET = 15:30 UTC
    # 3:00 ET  = 19:00 UTC | 4:00 ET  = 20:00 UTC
    morning = 13*60+25 <= mins <= 15*60+30
    close   = 19*60+0  <= mins <= 20*60+0
    return morning or close

def is_market_day():
    now = datetime.now(timezone.utc)
    # Mon-Fri only
    return now.weekday() < 5

def is_first_15min():
    now = datetime.now(timezone.utc)
    mins = now.hour * 60 + now.minute
    return 13*60+25 <= mins <= 13*60+45

def get_bars(sym, n=80, tf="5Min"):
    url = f"{DATA}/v2/stocks/{sym}/bars?timeframe={tf}&limit={n}&feed=iex"
    try:
        r = requests.get(url, headers=DH, timeout=10)
        return r.json().get("bars", [])
    except: return []

def rsi(c, p=14):
    if len(c) < p+1: return 50
    d = [c[i+1]-c[i] for i in range(len(c)-1)]
    g = [max(x,0) for x in d[-p:]]
    l = [abs(min(x,0)) for x in d[-p:]]
    ag, al = sum(g)/p, sum(l)/p
    return 100 if al == 0 else 100-(100/(1+ag/al))

def ema(closes, p):
    if not closes: return 0
    k = 2/(p+1); e = closes[0]
    for c in closes[1:]: e = c*k + e*(1-k)
    return e

def atr_val(bars, p=14):
    if len(bars) < 2: return 0.01
    trs = [max(bars[i]['h']-bars[i]['l'],
               abs(bars[i]['h']-bars[i-1]['c']),
               abs(bars[i]['l']-bars[i-1]['c']))
           for i in range(1, len(bars))]
    return sum(trs[-p:]) / min(len(trs), p)

def vwap(bars):
    last = bars[-50:] if len(bars) >= 50 else bars
    pv = sum(b['c']*b['v'] for b in last)
    v  = sum(b['v'] for b in last)
    return pv/v if v > 0 else bars[-1]['c']

def get_positions():
    try:
        r = requests.get(f"{BASE}/v2/positions", headers=H, timeout=10)
        return {p['symbol']: p for p in r.json()}
    except: return {}

def place_order(sym, side, qty):
    body = {"symbol": sym, "qty": str(int(qty)), "side": side,
            "type": "market", "time_in_force": "day"}
    try:
        r = requests.post(f"{BASE}/v2/orders", headers=H, json=body, timeout=10)
        return r.json()
    except: return {}

def close_pos(sym):
    try:
        requests.delete(f"{BASE}/v2/positions/{sym}", headers=H, timeout=10)
    except: pass

# In-memory state
positions  = {}
cooldowns  = {}
orb_levels = {}

def build_orb():
    global orb_levels
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if orb_levels.get("date") == today: return
    log("Building ORB levels...")
    new_orb = {"date": today}
    for sym in ASSETS:
        bars = get_bars(sym, 5, "5Min")
        if bars and len(bars) >= 3:
            first3 = bars[:3]
            new_orb[sym] = {
                "high": max(b['h'] for b in first3),
                "low":  min(b['l'] for b in first3)
            }
            log(f"  ORB {sym}: H={new_orb[sym]['high']:.2f} L={new_orb[sym]['low']:.2f}")
    orb_levels = new_orb

def scan_once():
    global positions, cooldowns, orb_levels
    now_ts = time.time()

    if not is_first_15min():
        build_orb()

    live = get_positions()

    # MANAGE OPEN POSITIONS
    to_close = []
    for sym, pos in list(positions.items()):
        if sym not in live:
            to_close.append(sym); continue
        bars = get_bars(sym, 80)
        if not bars: continue
        closes = [b['c'] for b in bars]
        curr = closes[-1]; entry = pos['entry']; side = pos['side']
        stop = pos['stop']; target = pos['target']
        bars_held = pos.get('bars_held', 0) + 1
        positions[sym]['bars_held'] = bars_held
        pnl_pct = ((curr-entry)/entry*100) if side == 'long' else ((entry-curr)/entry*100)
        pnl_usd = pnl_pct/100 * (pos['qty'] * entry)

        reason = None
        if side == 'long':
            if curr <= stop: reason = "STOP"
            elif curr >= target: reason = "TARGET"
        else:
            if curr >= stop: reason = "STOP"
            elif curr <= target: reason = "TARGET"
        if bars_held >= TIMEOUT: reason = "TIMEOUT"

        now_u = datetime.now(timezone.utc)
        if now_u.hour*60+now_u.minute >= 20*60: reason = "EOD"

        if reason:
            close_pos(sym)
            status = "WIN" if pnl_pct > 0 else "LOSS"
            log(f"CLOSED {sym} {side.upper()} | {reason} | PnL: {pnl_pct:+.3f}% | ${pnl_usd:+.2f}")
            log_trade_db(sym, side, entry, curr, pnl_usd, pnl_pct, status, reason)
            if reason == "STOP": cooldowns[sym] = now_ts + 1800
            to_close.append(sym)

    for sym in to_close: positions.pop(sym, None)

    if len(positions) >= 3:
        log("Max positions (3/3), skipping scan")
        return

    # SCAN FOR ENTRIES
    for sym in ASSETS:
        if sym in positions: continue
        if cooldowns.get(sym, 0) > now_ts: continue
        if len(positions) >= 3: break

        bars = get_bars(sym, 80)
        if not bars or len(bars) < 20: continue
        closes = [b['c'] for b in bars]
        curr = closes[-1]; prev = closes[-2]
        r_val = rsi(closes); a_val = atr_val(bars)
        vw    = vwap(bars)
        e9    = ema(closes[-30:], 9)
        e21   = ema(closes[-40:], 21)
        avg_v = sum(b['v'] for b in bars[-20:]) / 20
        vol_surge = bars[-1]['v'] >= avg_v * VOL_M

        signal = None; entry_type = ""
        orb = orb_levels.get(sym)
        if orb:
            if prev <= orb['high'] and curr > orb['high'] and r_val > 50 and vol_surge:
                signal = 'long'; entry_type = "ORB_BREAK"
            elif prev >= orb['low'] and curr < orb['low'] and r_val < 50 and vol_surge:
                signal = 'short'; entry_type = "ORB_BREAK"

        if not signal:
            if curr > vw and e9 > e21 and 45 <= r_val <= 75 and vol_surge and prev < vw:
                signal = 'long'; entry_type = "VWAP_CROSS"
            elif curr < vw and e9 < e21 and 25 <= r_val <= 55 and vol_surge and prev > vw:
                signal = 'short'; entry_type = "VWAP_CROSS"

        if not signal: continue

        stop_d = a_val * STOP_M; tgt_d = a_val * TGT_M
        stop_p = (curr - stop_d) if signal == 'long' else (curr + stop_d)
        tgt_p  = (curr + tgt_d)  if signal == 'long' else (curr - tgt_d)
        rr = tgt_d / stop_d
        if rr < MIN_RR: continue

        shares = max(int(RISK_USD / stop_d), 1)
        if shares * curr > MAX_POS: shares = int(MAX_POS / curr)
        if shares < 1: continue

        order = place_order(sym, signal, shares)
        if order.get('id'):
            positions[sym] = {'entry': curr, 'side': signal, 'stop': stop_p,
                               'target': tgt_p, 'qty': shares, 'bars_held': 0,
                               'rr': round(rr, 2), 'type': entry_type}
            log(f"ENTERED {sym} {signal.upper()} x{shares} | {entry_type} | "
                f"entry={curr:.2f} stop={stop_p:.2f} tgt={tgt_p:.2f} RR={rr:.2f}x")

def run():
    log("=== Nova Stocks Scalper — POWER HOURS MODE ===")
    log("Active windows: 9:25–11:30 AM ET | 3:00–4:00 PM ET")
    while True:
        if is_market_day() and is_power_hour():
            scan_once()
            time.sleep(SCAN_SEC)
        else:
            # Figure out next window
            now = datetime.now(timezone.utc)
            mins = now.hour * 60 + now.minute
            wday = now.weekday()
            if wday >= 5:
                log("Weekend — sleeping 1hr")
                time.sleep(3600)
            elif mins < 13*60+25:
                wait = (13*60+25 - mins) * 60
                log(f"Pre-market — sleeping {wait//60}min until 9:25 AM ET")
                time.sleep(min(wait, 3600))
            elif 15*60+30 < mins < 19*60:
                wait = (19*60 - mins) * 60
                log(f"Dead zone — sleeping {wait//60}min until 3:00 PM ET")
                time.sleep(min(wait, 3600))
            else:
                log("After hours — sleeping 1hr")
                time.sleep(3600)

if __name__ == "__main__":
    run()
