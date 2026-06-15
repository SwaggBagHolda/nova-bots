#!/usr/bin/env python3
"""
Nova FTMO Live Paper Trader
- Real intraday crypto bars via Alpaca (15-min candles, live prices)
- FTMO Phase 1 rules hard-enforced in code
- Strategy: breakout + momentum on ETH, BTC, XRP, AVAX, ADA
- NO overfitting: signal uses only current candle vs prior candles,
  no lookahead, no curve-fit params — just price action
- Logs to Base44 DB
"""
import requests, time, os
from datetime import datetime, timezone

# ── config ─────────────────────────────────────────────────────────────────────
ASSETS         = ["ETH/USD","BTC/USD","XRP/USD","AVAX/USD","ADA/USD"]
ACCOUNT        = 10_000.0
PROFIT_TARGET  = 0.10   # 10% = Phase 1 pass
MAX_TOTAL_DD   = 0.10   # 10% hard stop
MAX_DAILY_DD   = 0.05   # 5% daily hard stop
RISK_PCT       = 0.01   # 1% per trade
RR             = 2.0    # 2:1 R:R — conservative, not curve-fit
ATR_MULT_STOP  = 1.5    # stop = 1.5x ATR — standard, not optimized
MIN_TRADE_DAYS = 4
MAX_OPEN       = 3
SCAN_SEC       = 120
TF             = "15Min"
BARS_NEEDED    = 30

KEY = os.environ.get("ALPACA_API_KEY","PKHFMGMEDX45XRMPT4OWYIKKR4")
SEC = os.environ.get("ALPACA_SECRET","DqVA62jPiJuF88VmnusrDAQxBdsgnDahvQWuXiUcdt62")
H   = {"APCA-API-KEY-ID":KEY,"APCA-API-SECRET-KEY":SEC}
B44 = os.environ.get("BASE44_SERVICE_TOKEN","")
B44_URL = "https://api.base44.com/api/apps/69bf82ce8c526c379bdab3ce/entities/ScalperTrade"

# ── state ──────────────────────────────────────────────────────────────────────
equity      = ACCOUNT
peak_equity = ACCOUNT
daily_start = ACCOUNT
trade_days  = set()
positions   = {}
phase       = 1
passed      = False
last_day    = None

def log(m): print(f"[FTMO {datetime.now(timezone.utc).strftime('%H:%M')}] {m}",flush=True)

def log_db(sym,sig,entry,exit_p,pnl_usd,pnl_pct,status,reason):
    if not B44: return
    try:
        requests.post(B44_URL,json={
            "bot_name":"FTMO Live Paper","symbol":sym.replace("/",""),
            "scan_time":datetime.now(timezone.utc).isoformat(),
            "signal":sig,"entry_price":round(entry,4),"exit_price":round(exit_p,4),
            "pnl_usd":round(pnl_usd,2),"pnl_pct":round(pnl_pct,4),
            "trade_status":status,"reason":reason
        },headers={"Authorization":f"Bearer {B44}","Content-Type":"application/json"},timeout=8)
    except: pass

# ── data ───────────────────────────────────────────────────────────────────────
def get_bars(sym, n=BARS_NEEDED):
    url = f"https://data.alpaca.markets/v1beta3/crypto/us/bars?symbols={sym}&timeframe={TF}&limit={n}"
    try:
        r = requests.get(url, headers=H, timeout=10)
        return r.json().get("bars",{}).get(sym,[])
    except: return []

# ── signal (no overfitting) ────────────────────────────────────────────────────
def get_signal(bars):
    """
    Pure price action — no curve-fit parameters.
    Long:  current close breaks above 20-bar high AND close > EMA20 AND RSI not overbought
    Short: current close breaks below 20-bar low  AND close < EMA20 AND RSI not oversold
    Uses only prior bars to define levels — zero lookahead.
    """
    if len(bars) < 22: return None, None, None
    closes = [b["c"] for b in bars]
    curr   = closes[-1]

    # 20-bar high/low using only bars BEFORE current candle
    prior  = closes[:-1]
    hi20   = max(prior[-20:])
    lo20   = min(prior[-20:])

    # EMA20 on prior closes — standard, not optimized
    k   = 2/21
    ema = prior[0]
    for c in prior[1:]: ema = c*k + ema*(1-k)

    # RSI14 on prior closes
    d  = [prior[i+1]-prior[i] for i in range(len(prior)-1)]
    g  = [max(x,0) for x in d[-14:]]
    l  = [abs(min(x,0)) for x in d[-14:]]
    ag,al = sum(g)/14, sum(l)/14
    rsi = 100 if al==0 else 100-(100/(1+ag/al))

    # ATR14 on prior bars
    br = bars[:-1]
    trs = [max(br[i]["h"]-br[i]["l"],
               abs(br[i]["h"]-br[i-1]["c"]),
               abs(br[i]["l"]-br[i-1]["c"])) for i in range(1,len(br))]
    atr = sum(trs[-14:])/14 if len(trs)>=14 else None

    signal = None
    # Breakout long: fresh break of 20-bar high, price above EMA, RSI not >70
    if curr > hi20 and curr > ema and rsi < 70:
        signal = "long"
    # Breakout short: fresh break of 20-bar low, price below EMA, RSI not <30
    elif curr < lo20 and curr < ema and rsi > 30:
        signal = "short"

    return signal, atr, curr

# ── FTMO rules ─────────────────────────────────────────────────────────────────
def within_rules():
    total_dd = (ACCOUNT - equity) / ACCOUNT
    daily_dd = (daily_start - equity) / max(daily_start, 1)
    if total_dd >= MAX_TOTAL_DD:
        log(f"🚨 TOTAL DD {total_dd*100:.1f}% — HALTED"); return False
    if daily_dd >= MAX_DAILY_DD:
        log(f"🚨 DAILY DD {daily_dd*100:.1f}% — HALTED TODAY"); return False
    return True

def check_phase_complete():
    global phase, passed, equity
    profit = (equity - ACCOUNT) / ACCOUNT
    target = PROFIT_TARGET if phase == 1 else 0.05
    if profit >= target and len(trade_days) >= MIN_TRADE_DAYS:
        log(f"🏆 PHASE {phase} COMPLETE — profit={profit*100:.1f}% days={len(trade_days)}")
        if phase == 1:
            phase = 2
            log("Advancing to Phase 2 — target 5%")
        else:
            passed = True
            log("🎉 FTMO CHALLENGE PASSED — ready for live funded account!")

# ── trade management ───────────────────────────────────────────────────────────
def close_trade(sym, reason, price):
    global equity, peak_equity
    pos = positions.pop(sym, None)
    if not pos: return
    if pos["side"] == "long":
        pnl = (price - pos["entry"]) / pos["atr"] * pos["risk_usd"] / ATR_MULT_STOP
    else:
        pnl = (pos["entry"] - price) / pos["atr"] * pos["risk_usd"] / ATR_MULT_STOP
    equity += pnl
    peak_equity = max(peak_equity, equity)
    pct = pnl/ACCOUNT*100
    status = "WIN" if pnl>0 else "LOSS"
    total_p = (equity-ACCOUNT)/ACCOUNT*100
    log(f"CLOSED {sym} {pos['side'].upper()} | {reason} | ${pnl:+.2f} ({pct:+.2f}%) | equity=${equity:.0f} total={total_p:+.2f}%")
    log_db(sym.replace("/",""), pos["side"].upper(), pos["entry"], price, pnl, pct, status, reason)
    check_phase_complete()

def run():
    global equity, peak_equity, daily_start, last_day
    log("=== Nova FTMO Live Paper — Phase 1 ===")
    log(f"${ACCOUNT:.0f} | Target +10% | MaxDD 10% | Daily -5% | Risk 1%/trade | RR 2:1")
    log("Assets: " + ", ".join(ASSETS))
    log("Signal: 20-bar breakout + EMA20 + RSI filter | NO curve-fitting")

    while True:
        today = datetime.now().strftime("%Y-%m-%d")
        if today != last_day:
            daily_start = equity
            last_day = today
            log(f"📅 Day reset — equity=${equity:.0f} profit={(equity-ACCOUNT)/ACCOUNT*100:+.2f}%")

        if passed:
            log("Challenge passed — awaiting live account"); time.sleep(3600); continue

        if not within_rules():
            time.sleep(3600); continue

        trade_days.add(today)

        # Manage open positions
        for sym in list(positions.keys()):
            bars = get_bars(sym)
            if not bars: continue
            price = bars[-1]["c"]
            pos   = positions[sym]
            bars_held = pos.get("bars",0)+1
            positions[sym]["bars"] = bars_held
            hit_stop = price<=pos["stop"] if pos["side"]=="long" else price>=pos["stop"]
            hit_tgt  = price>=pos["target"] if pos["side"]=="long" else price<=pos["target"]
            live_pnl = ((price-pos["entry"]) if pos["side"]=="long" else (pos["entry"]-price))
            log(f"  HOLD {sym} {pos['side'].upper()} bar={bars_held} price={price:.4f} live_pnl={live_pnl:+.4f}")
            if hit_stop:       close_trade(sym,"STOP",price)
            elif hit_tgt:      close_trade(sym,"TARGET",price)
            elif bars_held>=48: close_trade(sym,"TIMEOUT",price)
            time.sleep(0.5)

        # Scan for entries
        if len(positions) < MAX_OPEN:
            for sym in ASSETS:
                if sym in positions: continue
                if len(positions) >= MAX_OPEN: break
                if not within_rules(): break
                bars = get_bars(sym)
                if not bars or len(bars)<BARS_NEEDED: continue
                signal, atr, price = get_signal(bars)
                if not signal or not atr: continue
                stop_dist = atr * ATR_MULT_STOP
                risk_usd  = equity * RISK_PCT
                stop_p  = (price-stop_dist) if signal=="long" else (price+stop_dist)
                target_p= (price+stop_dist*RR) if signal=="long" else (price-stop_dist*RR)
                positions[sym] = {"entry":price,"side":signal,"stop":stop_p,
                                  "target":target_p,"risk_usd":risk_usd,"atr":atr,"bars":0}
                p_pct = (equity-ACCOUNT)/ACCOUNT*100
                dd_pct= (peak_equity-equity)/peak_equity*100 if peak_equity>0 else 0
                log(f"📈 ENTERED {sym} {signal.upper()} @ {price:.4f} | stop={stop_p:.4f} tgt={target_p:.4f} risk=${risk_usd:.0f} | profit={p_pct:+.2f}% dd={dd_pct:.2f}%")
                time.sleep(0.5)

        time.sleep(SCAN_SEC)

if __name__=="__main__":
    run()
