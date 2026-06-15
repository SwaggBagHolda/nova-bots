import nova_logger
#!/usr/bin/env python3
"""
Nova Options Trader — Alpaca Paper Trading
Strategy: 0DTE / Weekly SPY + QQQ Options
- Buys calls/puts based on trend + momentum signals
- Power hours only: 9:30–11:30 AM ET, 3:00–3:45 PM ET
- Targets: 50% gain | Stop: 30% loss | Max 3 contracts open
- Uses ATM options (closest strike to current price)
- Logs all trades to Base44 DB
"""
import requests, json, time, os
from datetime import datetime, timezone, timedelta

KEY  = os.environ.get("ALPACA_API_KEY",  "PKHFMGMEDX45XRMPT4OWYIKKR4")
SEC  = os.environ.get("ALPACA_SECRET",   "DqVA62jPiJuF88VmnusrDAQxBdsgnDahvQWuXiUcdt62")
BASE = "https://paper-api.alpaca.markets"
DATA = "https://data.alpaca.markets"
H    = {"APCA-API-KEY-ID": KEY, "APCA-API-SECRET-KEY": SEC, "Content-Type": "application/json"}
DH   = {"APCA-API-KEY-ID": KEY, "APCA-API-SECRET-KEY": SEC}

UNDERLYINGS = ["SPY", "QQQ"]
MAX_POSITIONS = 3
TARGET_PCT    = 0.50   # 50% gain
STOP_PCT      = 0.30   # 30% loss
SCAN_SEC      = 120
CONTRACTS     = 1      # 1 contract = 100 shares

B44_TOKEN = os.environ.get("BASE44_SERVICE_TOKEN", "")
B44_URL   = "https://app.base44.com/api/apps/69bf82ce8c526c379bdab3ce/entities/ScalperTrade"

def log(msg):
    print(f"[OPTIONS {datetime.now().strftime('%H:%M')}] {msg}", flush=True)

def log_db(sym, signal, entry, exit_p, pnl_usd, pnl_pct, status, reason):
    if not B44_TOKEN: return
    try:
        requests.post(B44_URL, json={
            "bot_name": "Options Trader",
            "symbol": sym, "scan_time": datetime.now(timezone.utc).isoformat(),
            "signal": signal, "entry_price": round(entry, 4),
            "exit_price": round(exit_p, 4), "pnl_usd": round(pnl_usd, 2),
            "pnl_pct": round(pnl_pct, 4), "trade_status": status, "reason": reason
        }, headers={"Authorization": f"Bearer {B44_TOKEN}", "Content-Type": "application/json"}, timeout=8)
    except: pass

def is_power_hour():
    now = datetime.now(timezone.utc)
    m = now.hour * 60 + now.minute
    return (13*60+30 <= m <= 15*60+30) or (19*60 <= m <= 19*60+45)

def is_market_day():
    return datetime.now(timezone.utc).weekday() < 5

def get_bars(sym, n=50, tf="5Min"):
    url = f"{DATA}/v2/stocks/{sym}/bars?timeframe={tf}&limit={n}&feed=iex"
    try:
        r = requests.get(url, headers=DH, timeout=10)
        return r.json().get("bars", [])
    except: return []

def ema(closes, p):
    if not closes: return 0
    k = 2/(p+1); e = closes[0]
    for c in closes[1:]: e = c*k + e*(1-k)
    return e

def rsi(c, p=14):
    if len(c) < p+1: return 50
    d = [c[i+1]-c[i] for i in range(len(c)-1)]
    g = [max(x,0) for x in d[-p:]]; l = [abs(min(x,0)) for x in d[-p:]]
    ag, al = sum(g)/p, sum(l)/p
    return 100 if al==0 else 100-(100/(1+ag/al))

def vwap(bars):
    last = bars[-30:] if len(bars) >= 30 else bars
    pv = sum(b['c']*b['v'] for b in last)
    v  = sum(b['v'] for b in last)
    return pv/v if v > 0 else bars[-1]['c']

def get_atm_option(sym, direction, expiry=None):
    """Find ATM call or put for today or nearest expiry"""
    try:
        bars = get_bars(sym, 5, "5Min")
        if not bars: return None
        price = bars[-1]['c']
        strike = round(price / 5) * 5  # round to nearest $5

        if not expiry:
            expiry = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        opt_type = "call" if direction == "CALL" else "put"
        url = (f"{BASE}/v2/options/contracts?underlying_symbols={sym}"
               f"&type={opt_type}&strike_price_gte={strike-10}&strike_price_lte={strike+10}"
               f"&expiration_date_gte={expiry}&limit=20")
        r = requests.get(url, headers=H, timeout=10)
        contracts = r.json().get("option_contracts", [])

        if not contracts: return None
        # Pick ATM — closest strike to current price
        best = min(contracts, key=lambda c: abs(float(c['strike_price']) - price))
        return best
    except Exception as e:
        log(f"get_atm_option error: {e}")
        return None

def get_option_quote(symbol):
    try:
        url = f"{DATA}/v2/options/trades/latest?symbols={symbol}"
        r = requests.get(url, headers=DH, timeout=10)
        trades = r.json().get("trades", {})
        if symbol in trades:
            return float(trades[symbol]['p'])
        # fallback to contract close price
        url2 = f"{BASE}/v2/options/contracts/{symbol}"
        r2 = requests.get(url2, headers=H, timeout=10)
        c = r2.json()
        return float(c.get('close_price', 1.0))
    except: return None

def get_open_positions():
    try:
        r = requests.get(f"{BASE}/v2/positions", headers=H, timeout=10)
        data = r.json()
        if isinstance(data, list):
            return {p['symbol']: p for p in data}
        return {}
    except: return {}

def place_option_order(symbol, side, qty=1):
    body = {"symbol": symbol, "qty": str(qty), "side": side,
            "type": "market", "time_in_force": "day"}
    try:
        r = requests.post(f"{BASE}/v2/orders", headers=H, json=body, timeout=10)
        return r.json()
    except Exception as e:
        return {"error": str(e)}

def close_option(symbol):
    try:
        requests.delete(f"{BASE}/v2/positions/{symbol}", headers=H, timeout=10)
    except: pass

# In-memory open option positions
open_opts = {}  # symbol -> {entry_price, direction, underlying, qty, contract_symbol}

def scan_once():
    global open_opts
    live = get_open_positions()

    # MANAGE OPEN OPTIONS
    to_close = []
    for sym, pos in list(open_opts.items()):
        csym = pos['contract_symbol']
        price = get_option_quote(csym)
        if not price:
            to_close.append(sym); continue

        entry = pos['entry_price']
        pnl_pct = (price - entry) / entry
        pnl_usd = pnl_pct * entry * 100 * pos['qty']

        now = datetime.now(timezone.utc)
        mins = now.hour * 60 + now.minute
        eod = mins >= 19*60+40  # close by 3:40 PM ET

        reason = None
        if pnl_pct >= TARGET_PCT:  reason = "TARGET"
        elif pnl_pct <= -STOP_PCT: reason = "STOP"
        elif eod:                  reason = "EOD"

        if reason:
            close_option(csym)
            status = "WIN" if pnl_pct > 0 else "LOSS"
            log(f"CLOSED {pos['direction']} {sym} | {reason} | PnL: {pnl_pct*100:+.1f}% | ${pnl_usd:+.2f}")
            log_db(sym, pos['direction'], entry, price,
                   pnl_usd, round(pnl_pct*100, 2), status, reason)
            to_close.append(sym)

    for sym in to_close: open_opts.pop(sym, None)

    if len(open_opts) >= MAX_POSITIONS: return

    # SCAN FOR ENTRIES
    for sym in UNDERLYINGS:
        if sym in open_opts: continue
        if len(open_opts) >= MAX_POSITIONS: break

        bars = get_bars(sym, 60)
        if not bars or len(bars) < 20: continue
        closes = [b['c'] for b in bars]
        curr = closes[-1]
        e9   = ema(closes[-20:], 9)
        e21  = ema(closes[-30:], 21)
        r_val = rsi(closes)
        vw    = vwap(bars)
        avg_v = sum(b['v'] for b in bars[-10:]) / 10
        vol_surge = bars[-1]['v'] >= avg_v * 1.3

        # Use PRIOR bars only for indicators — no lookahead
        prior_bars   = bars[:-1]
        prior_closes = [b['c'] for b in prior_bars]
        prior_vw  = vwap(prior_bars)
        prior_e9  = ema(prior_closes[-20:], 9)
        prior_e21 = ema(prior_closes[-30:], 21)
        prior_rsi = rsi(prior_closes)
        avg_v_p   = sum(b['v'] for b in prior_bars[-10:]) / 10
        vol_surge = bars[-1]['v'] >= avg_v_p * 1.3

        direction = None
        # CALL: price breaks above prior VWAP, EMAs trending up, RSI not overbought
        if curr > prior_vw and prior_e9 > prior_e21 and 45 <= prior_rsi <= 68 and vol_surge:
            direction = "CALL"
        # PUT: price breaks below prior VWAP, EMAs trending down, RSI not oversold
        elif curr < prior_vw and prior_e9 < prior_e21 and 32 <= prior_rsi <= 55 and vol_surge:
            direction = "PUT"

        if not direction:
            log(f"{sym}: no signal | RSI={r_val:.0f} VWAP={vw:.2f} curr={curr:.2f}")
            continue

        contract = get_atm_option(sym, direction)
        if not contract:
            log(f"{sym}: no ATM contract found for {direction}")
            continue

        csym = contract['symbol']
        price = get_option_quote(csym)
        if not price or price < 0.05:
            log(f"{sym}: bad quote on {csym}: {price}")
            continue

        order = place_option_order(csym, "buy", CONTRACTS)
        if order.get('id'):
            open_opts[sym] = {
                'contract_symbol': csym, 'entry_price': price,
                'direction': direction, 'underlying': sym, 'qty': CONTRACTS
            }
            log(f"ENTERED {direction} on {sym} via {csym} @ ${price:.2f} | RSI={r_val:.0f}")
        else:
            log(f"{sym}: order failed — {order.get('message', order)}")

def run():
    log("=== Nova Options Trader — STARTING ===")
    log("SPY + QQQ | 0DTE/Weekly Calls & Puts | Power Hours")
    while True:
        if is_market_day() and is_power_hour():
            scan_once()
            time.sleep(SCAN_SEC)
        else:
            now = datetime.now(timezone.utc)
            m = now.hour * 60 + now.minute
            wday = now.weekday()
            if wday >= 5:
                time.sleep(3600)
            elif m < 13*60+30:
                wait = (13*60+30 - m) * 60
                log(f"Pre-market — waiting {wait//60}min")
                time.sleep(min(wait, 1800))
            elif 15*60+30 < m < 19*60:
                wait = (19*60 - m) * 60
                log(f"Dead zone — waiting {wait//60}min")
                time.sleep(min(wait, 1800))
            else:
                time.sleep(1800)

if __name__ == "__main__":
    run()
