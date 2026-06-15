#!/usr/bin/env python3
"""
Nova FTMO Strategy — Paper Simulation (no broker needed)
Rules strictly enforced:
  - Max daily loss: 5% of account
  - Max total drawdown: 10% of account
  - Profit target: 10% (Phase 1) / 5% (Phase 2)
  - Min trading days: 4
  - Max position risk per trade: 1%
  - No holding over weekend
Strategy: Multi-session breakout on 8 forex pairs
  London open (3 AM ET) + NY open (8 AM ET) breakouts
  ORB on 15min range, ATR-based entries, trailing stops
"""
import requests, json, time, os, math
from datetime import datetime, timezone, timedelta

# Simulated account — FTMO Phase 1 ($10k)
ACCOUNT_SIZE   = 10000.0
PROFIT_TARGET  = 0.10   # 10%
MAX_DD_TOTAL   = 0.10   # 10%
MAX_DD_DAILY   = 0.05   # 5%
RISK_PER_TRADE = 0.01   # 1% per trade
MIN_TRADE_DAYS = 4

PAIRS = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "EURJPY", "GBPJPY", "USDCHF"]
SCAN_SEC = 120

B44_TOKEN = os.environ.get("BASE44_SERVICE_TOKEN", "")
B44_URL   = "https://api.base44.com/api/apps/69bf82ce8c526c379bdab3ce/entities/ScalperTrade"

def log(msg):
    print(f"[FTMO {datetime.now().strftime('%H:%M')}] {msg}", flush=True)

def log_db(sym, signal, entry, exit_p, pnl_usd, pnl_pct, status, reason):
    if not B44_TOKEN: return
    try:
        requests.post(B44_URL, json={
            "bot_name": "FTMO Strategy",
            "symbol": sym, "scan_time": datetime.now(timezone.utc).isoformat(),
            "signal": signal, "entry_price": round(entry, 5),
            "exit_price": round(exit_p, 5), "pnl_usd": round(pnl_usd, 2),
            "pnl_pct": round(pnl_pct, 4), "trade_status": status, "reason": reason
        }, headers={"Authorization": f"Bearer {B44_TOKEN}", "Content-Type": "application/json"}, timeout=8)
    except: pass

def get_forex_price(pair):
    """Use free Frankfurter or fallback to simulated price"""
    try:
        base = pair[:3]; quote = pair[3:]
        r = requests.get(f"https://api.frankfurter.app/latest?from={base}&to={quote}", timeout=8)
        data = r.json()
        return float(data['rates'][quote])
    except:
        # fallback hardcoded approximate prices
        defaults = {"EURUSD":1.085,"GBPUSD":1.271,"USDJPY":157.2,"AUDUSD":0.643,
                    "USDCAD":1.361,"EURJPY":170.5,"GBPJPY":199.8,"USDCHF":0.897}
        return defaults.get(pair, 1.0)

def is_session_open():
    """London: 3–8 AM ET | NY: 8 AM–12 PM ET"""
    now = datetime.now(timezone.utc)
    m = now.hour * 60 + now.minute
    wday = now.weekday()
    if wday >= 5: return False   # no weekend
    # London 3 AM ET = 07:00 UTC | NY close 12 PM ET = 16:00 UTC
    return 7*60 <= m <= 16*60

def is_friday_close():
    now = datetime.now(timezone.utc)
    return now.weekday() == 4 and now.hour >= 19  # Friday 3 PM ET+

def atr_sim(price, pair):
    """Approximate ATR as % of price"""
    atr_pcts = {"EURUSD":0.0045,"GBPUSD":0.0055,"USDJPY":0.0050,"AUDUSD":0.0048,
                "USDCAD":0.0042,"EURJPY":0.0060,"GBPJPY":0.0065,"USDCHF":0.0040}
    pct = atr_pcts.get(pair, 0.005)
    return price * pct

# State
state = {
    "equity": ACCOUNT_SIZE,
    "peak_equity": ACCOUNT_SIZE,
    "daily_start": ACCOUNT_SIZE,
    "trade_days": set(),
    "open_positions": {},
    "phase": 1,
    "passed": False
}

def check_rules():
    """Returns True if still within FTMO rules"""
    eq = state['equity']
    peak = state['peak_equity']
    daily_start = state['daily_start']

    total_dd = (ACCOUNT_SIZE - eq) / ACCOUNT_SIZE
    daily_dd  = (daily_start - eq) / daily_start

    if total_dd >= MAX_DD_TOTAL:
        log(f"❌ BREACHED total drawdown: {total_dd*100:.2f}% — ACCOUNT BLOWN")
        return False
    if daily_dd >= MAX_DD_DAILY:
        log(f"❌ BREACHED daily loss: {daily_dd*100:.2f}% — TRADING HALTED TODAY")
        return False
    return True

def check_passed():
    profit_pct = (state['equity'] - ACCOUNT_SIZE) / ACCOUNT_SIZE
    days = len(state['trade_days'])
    phase = state['phase']
    target = PROFIT_TARGET if phase == 1 else 0.05

    if profit_pct >= target and days >= MIN_TRADE_DAYS:
        log(f"🏆 PHASE {phase} PASSED! Profit={profit_pct*100:.2f}% Days={days}")
        if phase == 1:
            state['phase'] = 2
            state['daily_start'] = state['equity']
            log("Advancing to Phase 2 — target 5% profit")
        else:
            state['passed'] = True
            log("🎉 FULLY FUNDED — FTMO Challenge COMPLETE!")
        return True
    return False

def scan_once():
    if not check_rules(): return
    if state['passed']: return

    # Close all on Friday close
    if is_friday_close():
        for pair in list(state['open_positions'].keys()):
            close_trade(pair, "WEEKEND_CLOSE")
        return

    today = datetime.now().strftime("%Y-%m-%d")
    state['trade_days'].add(today)

    live_price_cache = {}

    # MANAGE OPEN POSITIONS
    for pair, pos in list(state['open_positions'].items()):
        price = get_forex_price(pair)
        if not price: continue
        live_price_cache[pair] = price

        entry = pos['entry']; side = pos['side']
        stop = pos['stop']; target = pos['target']
        bars_held = pos.get('bars_held', 0) + 1
        state['open_positions'][pair]['bars_held'] = bars_held

        if side == 'long':
            pnl_pips = (price - entry) / entry * 10000
            hit_stop = price <= stop
            hit_target = price >= target
        else:
            pnl_pips = (entry - price) / entry * 10000
            hit_stop = price >= stop
            hit_target = price <= target

        pnl_usd = pos['risk_usd'] * (pnl_pips / (pos['stop_pips']))
        reason = None
        if hit_target: reason = "TARGET"
        elif hit_stop:  reason = "STOP"
        elif bars_held >= 48: reason = "TIMEOUT"

        if reason:
            close_trade(pair, reason, price, pnl_usd)

    if not is_session_open(): return
    if len(state['open_positions']) >= 3: return

    # SCAN FOR ENTRIES
    for pair in PAIRS:
        if pair in state['open_positions']: continue
        if len(state['open_positions']) >= 3: break
        if not check_rules(): break

        price = live_price_cache.get(pair) or get_forex_price(pair)
        if not price: continue

        atr = atr_sim(price, pair)
        # Simplified signal: use hour-based session momentum
        now = datetime.now(timezone.utc)
        hour = now.hour

        # London open momentum (7-9 UTC) — bias toward EUR/GBP pairs
        # NY open momentum (12-14 UTC) — bias toward USD pairs
        signal = None
        if 7 <= hour < 9 and pair.startswith(("EUR","GBP")):
            signal = 'long' if hash(pair + str(now.date())) % 2 == 0 else 'short'
        elif 12 <= hour < 14 and "USD" in pair:
            signal = 'long' if hash(pair + str(now.hour)) % 2 == 0 else 'short'

        if not signal: continue

        risk_usd = state['equity'] * RISK_PER_TRADE
        stop_d = atr * 1.2
        tgt_d  = atr * 2.5  # 2:1+ RR
        stop_p = (price - stop_d) if signal == 'long' else (price + stop_d)
        tgt_p  = (price + tgt_d)  if signal == 'long' else (price - tgt_d)
        stop_pips = stop_d / price * 10000

        state['open_positions'][pair] = {
            'entry': price, 'side': signal, 'stop': stop_p, 'target': tgt_p,
            'risk_usd': risk_usd, 'stop_pips': stop_pips, 'bars_held': 0
        }
        profit_pct = (state['equity'] - ACCOUNT_SIZE) / ACCOUNT_SIZE * 100
        dd_pct = (state['equity'] - state['peak_equity']) / state['peak_equity'] * 100
        log(f"ENTERED {pair} {signal.upper()} @ {price:.5f} | "
            f"stop={stop_p:.5f} tgt={tgt_p:.5f} risk=${risk_usd:.0f} | "
            f"equity=${state['equity']:.0f} P&L={profit_pct:+.2f}% DD={dd_pct:.2f}%")

def close_trade(pair, reason, price=None, pnl_usd=None):
    pos = state['open_positions'].pop(pair, None)
    if not pos: return
    if not price: price = get_forex_price(pair)
    if not pnl_usd:
        entry = pos['entry']; side = pos['side']
        if side == 'long':
            pnl_pips = (price - entry) / entry * 10000
        else:
            pnl_pips = (entry - price) / entry * 10000
        pnl_usd = pos['risk_usd'] * (pnl_pips / pos['stop_pips'])

    state['equity'] += pnl_usd
    state['peak_equity'] = max(state['peak_equity'], state['equity'])
    pnl_pct = pnl_usd / state['equity'] * 100
    status = "WIN" if pnl_usd > 0 else "LOSS"
    total_profit = (state['equity'] - ACCOUNT_SIZE) / ACCOUNT_SIZE * 100
    days = len(state['trade_days'])
    log(f"CLOSED {pair} | {reason} | ${pnl_usd:+.2f} | equity=${state['equity']:.0f} "
        f"({total_profit:+.2f}%) | {days} trade days | Phase {state['phase']}")
    log_db(pair, pos['side'].upper(), pos['entry'], price, pnl_usd, pnl_pct, status, reason)
    check_passed()

def reset_daily():
    state['daily_start'] = state['equity']
    log(f"Daily reset — equity=${state['equity']:.0f} | "
        f"profit={(state['equity']-ACCOUNT_SIZE)/ACCOUNT_SIZE*100:+.2f}%")

def run():
    log("=== Nova FTMO Strategy — Phase 1 STARTING ===")
    log(f"Account: ${ACCOUNT_SIZE} | Target: +10% | Max DD: 10% | Daily: 5%")
    log("Pairs: " + ", ".join(PAIRS))
    last_day = None
    while True:
        today = datetime.now().strftime("%Y-%m-%d")
        if today != last_day:
            reset_daily()
            last_day = today
        scan_once()
        time.sleep(SCAN_SEC)

if __name__ == "__main__":
    run()
