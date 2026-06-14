#!/usr/bin/env python3
"""
Nova Backtest + Fast Learning Engine
Karpathy-style compressed learning: runs 10,000+ simulated trades against
historical OANDA data to pre-train the candle predictor BEFORE live trading.
Covers: Scalp, Breakout, Trend, Reversal — Bull AND Bear.
"""

import os, json, time, requests, random
from datetime import datetime, timezone, timedelta
from collections import defaultdict

OANDA_TOKEN  = os.environ.get("OANDA_API_TOKEN", "")
BASE_URL     = "https://api-fxpractice.oanda.com"
HEADERS      = {"Authorization": f"Bearer {OANDA_TOKEN}"}
HISTORY_FILE = "/tmp/candle_predictor_history.json"
BRAIN_FILE   = "/tmp/nova_brain.json"

PAIRS = [
    "EUR_USD","GBP_USD","USD_JPY","AUD_USD",
    "USD_CAD","EUR_GBP","GBP_JPY","EUR_JPY"
]

TIMEFRAMES = ["M5", "M15", "H1"]  # Scalp / Swing / Trend

# ── Strategy Types ────────────────────────────────────────────────────────────
STRATEGIES = {
    "scalp_bull":      {"min_score": 6, "rr": 1.5, "atr_sl": 0.8,  "atr_tp": 1.2, "bars": 8},
    "scalp_bear":      {"min_score": 6, "rr": 1.5, "atr_sl": 0.8,  "atr_tp": 1.2, "bars": 8},
    "breakout_bull":   {"min_score": 7, "rr": 2.5, "atr_sl": 1.5,  "atr_tp": 3.5, "bars": 20},
    "breakout_bear":   {"min_score": 7, "rr": 2.5, "atr_sl": 1.5,  "atr_tp": 3.5, "bars": 20},
    "trend_bull":      {"min_score": 6, "rr": 2.0, "atr_sl": 1.2,  "atr_tp": 2.5, "bars": 30},
    "trend_bear":      {"min_score": 6, "rr": 2.0, "atr_sl": 1.2,  "atr_tp": 2.5, "bars": 30},
    "reversal_bull":   {"min_score": 7, "rr": 3.0, "atr_sl": 1.0,  "atr_tp": 3.0, "bars": 15},
    "reversal_bear":   {"min_score": 7, "rr": 3.0, "atr_sl": 1.0,  "atr_tp": 3.0, "bars": 15},
}

# ── Fetch historical candles ──────────────────────────────────────────────────
def fetch_candles(pair, granularity="M15", count=500):
    try:
        r = requests.get(
            f"{BASE_URL}/v3/instruments/{pair}/candles",
            headers=HEADERS,
            params={"count": count, "granularity": granularity, "price": "M"},
            timeout=10
        )
        return r.json().get("candles", [])
    except:
        return []

# ── Indicators ────────────────────────────────────────────────────────────────
def atr(candles, p=14):
    trs = []
    for i in range(1, len(candles)):
        h  = float(candles[i]["mid"]["h"])
        l  = float(candles[i]["mid"]["l"])
        pc = float(candles[i-1]["mid"]["c"])
        trs.append(max(h-l, abs(h-pc), abs(l-pc)))
    return sum(trs[-p:]) / p if len(trs) >= p else 0.001

def rsi(candles, p=14):
    closes = [float(c["mid"]["c"]) for c in candles]
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        gains.append(max(d,0)); losses.append(max(-d,0))
    if len(gains) < p: return 50
    ag = sum(gains[-p:])/p; al = sum(losses[-p:])/p
    return 100 - (100/(1+(ag/al))) if al else 100

def sma(closes, p): return sum(closes[-p:])/p if len(closes)>=p else closes[-1]

def adx_simple(candles, p=14):
    """Simplified ADX proxy using ATR ratio."""
    if len(candles) < p+1: return 20
    a = atr(candles, p)
    ranges = [float(c["mid"]["h"])-float(c["mid"]["l"]) for c in candles[-p:]]
    avg_range = sum(ranges)/p
    return min(100, (a/avg_range)*50) if avg_range else 20

# ── Universal Signal Scorer ──────────────────────────────────────────────────
def score_signal(candles, strategy):
    if len(candles) < 30: return 0, 0
    closes = [float(c["mid"]["c"]) for c in candles]
    opens  = [float(c["mid"]["o"]) for c in candles]
    highs  = [float(c["mid"]["h"]) for c in candles]
    lows   = [float(c["mid"]["l"]) for c in candles]
    vols   = [float(c.get("volume",100)) for c in candles]

    c, o, h, l = closes[-1], opens[-1], highs[-1], lows[-1]
    body = abs(c - o); rng = h - l
    body_pct = body/rng if rng else 0
    bullish = c > o

    a    = atr(candles)
    r    = rsi(candles)
    s20  = sma(closes, 20)
    s50  = sma(closes, 50) if len(closes)>=50 else s20
    adx  = adx_simple(candles)
    avg_vol = sum(vols[-20:])/20
    score = 0

    if "scalp" in strategy:
        # Tight, quick moves
        if body_pct > 0.55: score += 2
        if body > a * 1.0:  score += 1
        if vols[-1] > avg_vol * 1.1: score += 1
        if "bull" in strategy and r > 52 and bullish: score += 2
        if "bear" in strategy and r < 48 and not bullish: score += 2
        if adx > 20: score += 1
        if body < a * 2.5: score += 1  # Not overextended
        score = min(score, 8)

    elif "breakout" in strategy:
        # Big momentum bursts
        if body > a * 1.5: score += 3
        if body_pct > 0.65: score += 2
        if vols[-1] > avg_vol * 1.3: score += 1
        if "bull" in strategy and c > max(highs[-20:-1]) and bullish: score += 2
        if "bear" in strategy and c < min(lows[-20:-1]) and not bullish: score += 2
        if adx > 25: score += 1
        score = min(score, 10)

    elif "trend" in strategy:
        # Riding established moves
        if "bull" in strategy:
            if c > s20 > s50: score += 2
            if closes[-1] > closes[-3] > closes[-5]: score += 2
            if r > 50 and r < 75: score += 2
            if bullish: score += 1
        else:
            if c < s20 < s50: score += 2
            if closes[-1] < closes[-3] < closes[-5]: score += 2
            if r < 50 and r > 25: score += 2
            if not bullish: score += 1
        if adx > 22: score += 1
        score = min(score, 8)

    elif "reversal" in strategy:
        # Exhaustion + flip
        if "bull" in strategy:
            if r < 32: score += 3
            if l < min(lows[-10:-1]) and c > o: score += 3  # Hammer / pin bar
            if c > s20: score += 1
        else:
            if r > 68: score += 3
            if h > max(highs[-10:-1]) and c < o: score += 3  # Shooting star
            if c < s20: score += 1
        if body_pct > 0.5: score += 1
        score = min(score, 8)

    return score, a

# ── Simulate one trade ────────────────────────────────────────────────────────
def simulate_trade(candles, idx, strategy, cfg, a_val):
    """Simulate trade outcome using future candles as 'market'."""
    c_entry = float(candles[idx]["mid"]["c"])
    bull = "bull" in strategy

    sl_dist = a_val * cfg["atr_sl"]
    tp_dist = a_val * cfg["atr_tp"]
    sl = c_entry - sl_dist if bull else c_entry + sl_dist
    tp = c_entry + tp_dist if bull else c_entry - tp_dist

    # Walk forward bars
    max_bars = cfg["bars"]
    for i in range(idx+1, min(idx+1+max_bars, len(candles))):
        h = float(candles[i]["mid"]["h"])
        l = float(candles[i]["mid"]["l"])
        if bull:
            if l <= sl: return "loss", -1.0
            if h >= tp: return "win",  cfg["rr"]
        else:
            if h >= sl: return "loss", -1.0
            if l <= tp: return "win",  cfg["rr"]

    # Timed out — partial exit at close
    c_exit = float(candles[min(idx+max_bars, len(candles)-1)]["mid"]["c"])
    pnl = (c_exit - c_entry)/a_val if bull else (c_entry - c_exit)/a_val
    return "timeout", round(pnl * 0.5, 3)

# ── Main Backtest Loop ────────────────────────────────────────────────────────
def run_backtest(target_trades=2000):
    print(f"🧠 Nova Backtest Engine — target {target_trades} trades")
    print("=" * 60)

    brain = defaultdict(lambda: {
        "trades": 0, "wins": 0, "total_pnl": 0,
        "win_rate": 0, "avg_pnl": 0, "score_threshold": 6
    })

    total_trades = 0
    all_results  = []

    for pair in PAIRS:
        for tf in TIMEFRAMES:
            print(f"\n📊 Backtesting {pair} {tf}...")
            candles = fetch_candles(pair, tf, count=500)
            if len(candles) < 100:
                print(f"  ⚠️ Not enough candles ({len(candles)}), skipping")
                continue

            for strat, cfg in STRATEGIES.items():
                wins = losses = timeouts = 0
                pnl_total = 0
                trade_log = []

                # Slide window across all candles
                for idx in range(30, len(candles) - cfg["bars"] - 1):
                    window = candles[max(0, idx-50):idx+1]
                    score, a_val = score_signal(window, strat)

                    if score < cfg["min_score"] or a_val == 0:
                        continue

                    outcome, pnl = simulate_trade(candles, idx, strat, cfg, a_val)
                    pnl_total += pnl
                    trade_log.append({"outcome": outcome, "pnl": pnl, "score": score})

                    if outcome == "win":   wins += 1
                    elif outcome == "loss": losses += 1
                    else: timeouts += 1

                    total_trades += 1

                n = wins + losses + timeouts
                if n == 0: continue

                wr = wins / n
                avg_pnl = pnl_total / n
                key = f"{pair}_{tf}_{strat}"

                brain[key] = {
                    "trades": n, "wins": wins, "losses": losses,
                    "win_rate": round(wr, 3),
                    "avg_pnl": round(avg_pnl, 4),
                    "total_pnl": round(pnl_total, 3),
                    "score_threshold": cfg["min_score"],
                    "pair": pair, "timeframe": tf, "strategy": strat
                }

                all_results.append((key, wr, avg_pnl, n))
                print(f"  {strat:<18} | {n:>4} trades | WR: {wr:.0%} | Avg PnL: {avg_pnl:+.4f}")

    # ── Save brain ────────────────────────────────────────────────────────────
    brain_data = dict(brain)
    brain_data["_meta"] = {
        "total_trades_simulated": total_trades,
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "pairs": PAIRS,
        "timeframes": TIMEFRAMES,
        "strategies": list(STRATEGIES.keys())
    }

    with open(BRAIN_FILE, "w") as f:
        json.dump(brain_data, f, indent=2)

    # ── Rankings ─────────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("🏆 TOP STRATEGIES BY WIN RATE:")
    top = sorted(all_results, key=lambda x: x[1]*x[2], reverse=True)[:10]
    for key, wr, avg_pnl, n in top:
        print(f"  {key:<45} WR:{wr:.0%} AvgPnL:{avg_pnl:+.4f} ({n} trades)")

    print(f"\n✅ Backtest complete — {total_trades} trades simulated")
    print(f"📁 Brain saved to {BRAIN_FILE}")
    return brain_data

if __name__ == "__main__":
    run_backtest(target_trades=5000)
