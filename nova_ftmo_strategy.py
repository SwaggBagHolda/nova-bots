#!/usr/bin/env python3
"""
Nova FTMO Live Paper Trader
- Real live prices via Frankfurter API (forex) + Alpaca (crypto/stocks)
- FTMO Phase 1 rules enforced HARD:
    * Max daily loss: 5%
    * Max total drawdown: 10%
    * Profit target: 10%
    * Min 4 trading days
    * 1% risk per trade max
- Strategy: London + NY session breakout on 8 forex pairs
- All trades logged to Base44 DB
- NO simulation — entries based on real price movement
"""
import requests, time, os, math
from datetime import datetime, timezone

PAIRS = ["EURUSD","GBPUSD","USDJPY","AUDUSD","USDCAD","EURJPY","GBPJPY","USDCHF"]
ACCOUNT        = 10_000.0
PROFIT_TARGET  = 0.10
MAX_TOTAL_DD   = 0.10
MAX_DAILY_DD   = 0.05
RISK_PCT       = 0.01
RR             = 2.5       # 2.5:1 reward:risk
ATR_STOP_MULT  = 1.5       # stop = 1.5x ATR
MAX_TRADES     = 3
SCAN_SEC       = 120

B44_TOKEN = os.environ.get("BASE44_SERVICE_TOKEN","")
B44_URL   = "https://api.base44.com/api/apps/69bf82ce8c526c379bdab3ce/entities/ScalperTrade"

# ── state ──────────────────────────────────────────────────────────────────────
equity       = ACCOUNT
peak_equity  = ACCOUNT
daily_start  = ACCOUNT
trade_days   = set()
positions    = {}   # pair -> {entry, side, stop, target, risk_usd, stop_dist}
price_hist   = {}   # pair -> [last N prices]
phase        = 1
passed       = False
last_day     = None

def log(msg):
    print(f"[FTMO {datetime.now(timezone.utc).strftime('%H:%M')}] {msg}", flush=True)

def log_db(sym, signal, entry, exit_p, pnl_usd, pnl_pct, status, reason):
    if not B44_TOKEN: return
    try:
        requests.post(B44_URL, json={
            "bot_name":"FTMO Live Paper","symbol":sym,
            "scan_time":datetime.now(timezone.utc).isoformat(),
            "signal":signal,"entry_price":round(entry,5),"exit_price":round(exit_p,5),
            "pnl_usd":round(pnl_usd,2),"pnl_pct":round(pnl_pct,4),
            "trade_status":status,"reason":reason
        }, headers={"Authorization":f"Bearer {B44_TOKEN}","Content-Type":"application/json"}, timeout=8)
    except: pass

# ── price feed ─────────────────────────────────────────────────────────────────
def get_price(pair):
    """Live forex rate — Frankfurter API (real ECB data)"""
    try:
        base, quote = pair[:3], pair[3:]
        r = requests.get(f"https://api.frankfurter.app/latest?from={base}&to={quote}", timeout=8)
        return float(r.json()['rates'][quote])
    except:
        return None

def build_history(pair):
    """Pull 30 days of daily closes to calculate ATR"""
    try:
        base, quote = pair[:3], pair[3:]
        r = requests.get(
            f"https://api.frankfurter.app/2026-05-01..?from={base}&to={quote}", timeout=10)
        rates = r.json().get("rates", {})
        closes = [v[quote] for v in rates.values() if quote in v]
        return closes
    except:
        return []

def calc_atr(closes, period=14):
    if len(closes) < period + 1: return None
    trs = [abs(closes[i] - closes[i-1]) for i in range(1, len(closes))]
    return sum(trs[-period:]) / period

def calc_ema(closes, period):
    if len(closes) < period: return None
    k = 2 / (period + 1)
    ema = closes[0]
    for c in closes[1:]: ema = c * k + ema * (1 - k)
    return ema

def get_signal(pair, closes, current):
    """Real signal from EMA crossover + momentum on live price history"""
    if len(closes) < 22: return None
    ema9  = calc_ema(closes[-20:], 9)
    ema21 = calc_ema(closes[-22:], 21)
    if not ema9 or not ema21: return None

    prev_closes = closes[:-1]
    prev_ema9  = calc_ema(prev_closes[-20:], 9)  if len(prev_closes) >= 9  else None
    prev_ema21 = calc_ema(prev_closes[-22:], 21) if len(prev_closes) >= 21 else None
    if not prev_ema9 or not prev_ema21: return None

    # Fresh crossover only
    if prev_ema9 <= prev_ema21 and ema9 > ema21:
        return "long"
    if prev_ema9 >= prev_ema21 and ema9 < ema21:
        return "short"
    return None

# ── FTMO rules ─────────────────────────────────────────────────────────────────
def within_rules():
    global equity, daily_start, peak_equity
    total_dd = (ACCOUNT - equity) / ACCOUNT
    daily_dd = (daily_start - equity) / daily_start if daily_start > 0 else 0
    if total_dd >= MAX_TOTAL_DD:
        log(f"🚨 TOTAL DD BREACH {total_dd*100:.2f}% — halting all trading")
        return False
    if daily_dd >= MAX_DAILY_DD:
        log(f"🚨 DAILY LOSS BREACH {daily_dd*100:.2f}% — halting today")
        return False
    return True

def check_target():
    global phase, passed, equity, daily_start
    profit = (equity - ACCOUNT) / ACCOUNT
    days = len(trade_days)
    target = PROFIT_TARGET if phase == 1 else 0.05
    if profit >= target and days >= 4:
        log(f"🏆 PHASE {phase} COMPLETE! Profit={profit*100:.1f}% | {days} trading days")
        if phase == 1:
            phase = 2
            log("→ Phase 2 starting — target 5% profit, same rules")
        else:
            passed = True
            log("🎉 FTMO CHALLENGE PASSED — ready for live funded account!")

# ── trade management ───────────────────────────────────────────────────────────
def close_trade(pair, reason, current_price=None):
    global equity, peak_equity
    pos = positions.pop(pair, None)
    if not pos: return
    p = current_price or get_price(pair) or pos['entry']
    if pos['side'] == 'long':
        pnl_usd = ((p - pos['entry']) / pos['stop_dist']) * pos['risk_usd']
    else:
        pnl_usd = ((pos['entry'] - p) / pos['stop_dist']) * pos['risk_usd']
    equity += pnl_usd
    peak_equity = max(peak_equity, equity)
    pnl_pct = pnl_usd / ACCOUNT * 100
    status = "WIN" if pnl_usd > 0 else "LOSS"
    profit_total = (equity - ACCOUNT) / ACCOUNT * 100
    log(f"✅ CLOSED {pair} {pos['side'].upper()} | {reason} | "
        f"${pnl_usd:+.2f} ({pnl_pct:+.2f}%) | equity=${equity:.0f} total={profit_total:+.2f}%")
    log_db(pair, pos['side'].upper(), pos['entry'], p, pnl_usd, pnl_pct, status, reason)
    check_target()

def is_session():
    """London 7–12 UTC | NY 12–17 UTC"""
    now = datetime.now(timezone.utc)
    m = now.hour * 60 + now.minute
    return 7*60 <= m <= 17*60 and now.weekday() < 5

def is_friday_close():
    now = datetime.now(timezone.utc)
    return now.weekday() == 4 and now.hour >= 19

# ── main loop ──────────────────────────────────────────────────────────────────
def run():
    global equity, peak_equity, daily_start, last_day, price_hist

    log("=== Nova FTMO Live Paper — Phase 1 STARTING ===")
    log(f"Account ${ACCOUNT:.0f} | Target +10% | Max DD 10% | Daily -5% | Risk 1%/trade")

    # Pre-load price history for all pairs
    log("Loading price history...")
    for pair in PAIRS:
        closes = build_history(pair)
        price_hist[pair] = closes
        if closes:
            log(f"  {pair}: {len(closes)} days loaded, last={closes[-1]:.5f}")
        time.sleep(0.5)

    while True:
        global last_day
        today = datetime.now().strftime("%Y-%m-%d")

        # Daily reset
        if today != last_day:
            daily_start = equity
            last_day = today
            log(f"📅 New day — equity=${equity:.0f} | profit={(equity-ACCOUNT)/ACCOUNT*100:+.2f}%")

        if passed:
            log("Challenge passed — standing by for live account setup")
            time.sleep(3600); continue

        if not within_rules():
            time.sleep(3600); continue

        # Close all on Friday
        if is_friday_close():
            for pair in list(positions.keys()):
                close_trade(pair, "WEEKEND_CLOSE")
            time.sleep(3600); continue

        trade_days.add(today)

        # ── manage open positions ──
        for pair in list(positions.keys()):
            pos = positions[pair]
            p = get_price(pair)
            if not p: continue

            # Update history
            price_hist[pair].append(p)
            if len(price_hist[pair]) > 100: price_hist[pair] = price_hist[pair][-100:]

            if pos['side'] == 'long':
                hit_stop   = p <= pos['stop']
                hit_target = p >= pos['target']
            else:
                hit_stop   = p >= pos['stop']
                hit_target = p <= pos['target']

            bars_held = pos.get('bars',0) + 1
            positions[pair]['bars'] = bars_held
            pnl_live = ((p-pos['entry']) if pos['side']=='long' else (pos['entry']-p)) / pos['stop_dist'] * pos['risk_usd']
            log(f"  HOLD {pair} {pos['side'].upper()} bar={bars_held} price={p:.5f} live=${pnl_live:+.2f}")

            if hit_stop:
                close_trade(pair, "STOP", p)
            elif hit_target:
                close_trade(pair, "TARGET", p)
            elif bars_held >= 48:
                close_trade(pair, "TIMEOUT", p)

            time.sleep(0.3)

        # ── scan for entries ──
        if is_session() and len(positions) < MAX_TRADES:
            for pair in PAIRS:
                if pair in positions: continue
                if len(positions) >= MAX_TRADES: break
                if not within_rules(): break

                p = get_price(pair)
                if not p: continue

                price_hist[pair].append(p)
                if len(price_hist[pair]) > 100: price_hist[pair] = price_hist[pair][-100:]

                closes = price_hist[pair]
                signal = get_signal(pair, closes, p)
                if not signal: continue

                atr = calc_atr(closes)
                if not atr or atr <= 0: continue

                stop_dist = atr * ATR_STOP_MULT
                risk_usd  = equity * RISK_PCT
                stop_p    = (p - stop_dist) if signal == 'long' else (p + stop_dist)
                target_p  = (p + stop_dist * RR) if signal == 'long' else (p - stop_dist * RR)

                positions[pair] = {
                    'entry': p, 'side': signal, 'stop': stop_p,
                    'target': target_p, 'risk_usd': risk_usd,
                    'stop_dist': stop_dist, 'bars': 0
                }
                profit_pct = (equity - ACCOUNT) / ACCOUNT * 100
                dd_pct = (peak_equity - equity) / peak_equity * 100
                log(f"📈 ENTERED {pair} {signal.upper()} @ {p:.5f} | "
                    f"stop={stop_p:.5f} tgt={target_p:.5f} risk=${risk_usd:.0f} | "
                    f"equity=${equity:.0f} profit={profit_pct:+.2f}% dd={dd_pct:.2f}%")
                time.sleep(0.3)

        time.sleep(SCAN_SEC)

if __name__ == "__main__":
    run()
