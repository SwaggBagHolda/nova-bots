#!/usr/bin/env python3
"""
Nova Forward Test Engine
Runs the trained brain against the MOST RECENT candles (data the backtest never saw).
Simulates paper trades in real-time conditions at compressed speed.
Validates: does the brain actually work on fresh data?
"""

import os, json, requests, time
from datetime import datetime, timezone

OANDA_TOKEN = os.environ.get("OANDA_API_TOKEN", "")
BASE_URL    = "https://api-fxpractice.oanda.com"
HEADERS     = {"Authorization": f"Bearer {OANDA_TOKEN}"}
BRAIN_FILE  = "/tmp/nova_brain.json"
FWD_FILE    = "/tmp/forward_test_results.json"

def load_brain():
    try:
        with open(BRAIN_FILE) as f:
            return json.load(f)
    except:
        return {}

def fetch_candles(pair, granularity="M15", count=100):
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

def atr(candles, p=14):
    trs = []
    for i in range(1, len(candles)):
        h  = float(candles[i]["mid"]["h"])
        l  = float(candles[i]["mid"]["l"])
        pc = float(candles[i-1]["mid"]["c"])
        trs.append(max(h-l, abs(h-pc), abs(l-pc)))
    return sum(trs[-p:]) / p if len(trs) >= p else 0.001

def simulate_forward(candles, idx, direction, atr_val, rr=2.0, atr_sl=1.2, atr_tp=2.5, bars=20):
    entry = float(candles[idx]["mid"]["c"])
    bull  = "bull" in direction or direction == "call"
    sl = entry - atr_val*atr_sl if bull else entry + atr_val*atr_sl
    tp = entry + atr_val*atr_tp if bull else entry - atr_val*atr_tp
    for i in range(idx+1, min(idx+1+bars, len(candles))):
        h = float(candles[i]["mid"]["h"])
        l = float(candles[i]["mid"]["l"])
        if bull:
            if l <= sl: return "loss", -1.0
            if h >= tp: return "win", rr
        else:
            if h >= sl: return "loss", -1.0
            if l <= tp: return "win", rr
    exit_p = float(candles[min(idx+bars, len(candles)-1)]["mid"]["c"])
    pnl = (exit_p-entry)/atr_val if bull else (entry-exit_p)/atr_val
    return "timeout", round(pnl*0.4, 3)

def run_forward_test():
    print("🔬 Nova Forward Test Engine — Validating brain on fresh data")
    print("=" * 60)

    brain = load_brain()
    if not brain:
        print("❌ No brain file found — run backtest first")
        return

    # Get top profitable strategies from brain
    top_strategies = []
    for key, data in brain.items():
        if key.startswith("_"): continue
        if data.get("trades", 0) >= 10 and data.get("win_rate", 0) >= 0.40 and data.get("avg_pnl", 0) > 0:
            top_strategies.append((key, data))

    top_strategies.sort(key=lambda x: x[1]["win_rate"] * x[1]["avg_pnl"], reverse=True)
    top_strategies = top_strategies[:10]  # Top 10 only

    print(f"Testing top {len(top_strategies)} strategies from brain...")

    results = []
    total_trades = wins = losses = 0
    equity = 10000.0
    peak   = 10000.0

    for key, brain_data in top_strategies:
        parts = key.split("_", 2)
        if len(parts) < 3: continue
        pair = f"{parts[0]}_{parts[1]}"
        rest = parts[2].split("_", 1)
        if len(rest) < 2: continue
        tf, strat = rest[0], rest[1]

        candles = fetch_candles(pair, tf, count=200)
        if len(candles) < 50:
            print(f"  ⚠️ {key} — not enough candles")
            continue

        # Use last 100 bars (forward test window — unseen by backtest)
        fwd_candles = candles[-100:]

        strat_wins = strat_losses = 0
        strat_pnl = 0

        for idx in range(30, len(fwd_candles) - 25):
            window = fwd_candles[max(0, idx-50):idx+1]
            closes = [float(c["mid"]["c"]) for c in window]
            opens  = [float(c["mid"]["o"]) for c in window]
            body   = abs(closes[-1] - opens[-1])
            rng    = float(window[-1]["mid"]["h"]) - float(window[-1]["mid"]["l"])
            body_pct = body/rng if rng else 0
            a = atr(window)

            # Quick signal check matching brain strategy
            signal = False
            if "scalp" in strat and body_pct > 0.55 and body > a * 0.8:
                signal = True
            elif "breakout" in strat and body > a * 1.5 and body_pct > 0.65:
                signal = True
            elif "trend" in strat and len(closes) >= 20:
                sma20 = sum(closes[-20:])/20
                signal = (closes[-1] > sma20 and "bull" in strat) or (closes[-1] < sma20 and "bear" in strat)

            if not signal: continue

            outcome, pnl = simulate_forward(fwd_candles, idx, strat, a)
            risk_pct = 0.005
            dollar_pnl = equity * risk_pct * pnl
            equity += dollar_pnl
            peak = max(peak, equity)
            strat_pnl += pnl

            if outcome == "win":   strat_wins += 1;   wins += 1
            elif outcome == "loss": strat_losses += 1; losses += 1
            total_trades += 1

        n = strat_wins + strat_losses
        if n > 0:
            wr = strat_wins / n
            avg = strat_pnl / n
            results.append({
                "strategy": key,
                "trades": n,
                "win_rate": round(wr, 3),
                "avg_pnl": round(avg, 4),
                "backtest_wr": brain_data["win_rate"],
                "consistent": abs(wr - brain_data["win_rate"]) < 0.15
            })
            status = "✅ CONSISTENT" if abs(wr - brain_data["win_rate"]) < 0.15 else "⚠️ DRIFT"
            print(f"  {key:<45} FWD:{wr:.0%} vs BT:{brain_data['win_rate']:.0%}  {status}")

    total_n = wins + losses
    overall_wr = wins/total_n if total_n else 0
    dd = (peak - equity)/peak if peak else 0

    print(f"\n{'='*60}")
    print(f"📊 FORWARD TEST RESULTS:")
    print(f"   Total trades:    {total_trades}")
    print(f"   Win rate:        {overall_wr:.1%}")
    print(f"   Final equity:    ${equity:,.2f}")
    print(f"   Max drawdown:    {dd:.1%}")
    print(f"   Consistent strats: {sum(1 for r in results if r['consistent'])}/{len(results)}")

    # Save
    output = {
        "results": results,
        "summary": {
            "total_trades": total_trades,
            "win_rate": overall_wr,
            "final_equity": equity,
            "max_drawdown": dd,
            "tested_at": datetime.now(timezone.utc).isoformat()
        }
    }
    with open(FWD_FILE, "w") as f:
        json.dump(output, f, indent=2)

    print(f"\n✅ Forward test complete. Results saved.")
    return output

if __name__ == "__main__":
    run_forward_test()
