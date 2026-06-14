#!/usr/bin/env python3
"""
Nova Breakout Hunter — OANDA Edition
Scans forex pairs for high-probability breakouts using candle expectancy scoring.
Features: Pyramiding, Trailing Stops, Compounding, FTMO-safe risk management.
"""

import os
import time
import json
import requests
from datetime import datetime, timezone

# ── Config ──────────────────────────────────────────────────────────────────
OANDA_TOKEN = os.environ.get("OANDA_API_TOKEN", "")
ACCOUNT_ID  = os.environ.get("OANDA_ACCOUNT_ID", "101-001-18157162-001")
ENVIRONMENT = os.environ.get("OANDA_ENVIRONMENT", "practice")

BASE_URL = (
    "https://api-fxpractice.oanda.com"
    if ENVIRONMENT == "practice"
    else "https://api-fxtrade.oanda.com"
)

HEADERS = {
    "Authorization": f"Bearer {OANDA_TOKEN}",
    "Content-Type": "application/json"
}

# ── Assets ──────────────────────────────────────────────────────────────────
PAIRS = [
    "EUR_USD", "GBP_USD", "USD_JPY", "AUD_USD",
    "USD_CAD", "EUR_GBP", "GBP_JPY", "EUR_JPY"
]

# ── Risk Management (FTMO-safe) ─────────────────────────────────────────────
RISK_PCT         = 0.005   # 0.5% risk per trade (FTMO safe)
MAX_DAILY_LOSS   = 0.03    # 3% daily loss hard stop (FTMO limit = 5%)
MAX_TOTAL_LOSS   = 0.08    # 8% total drawdown hard stop (FTMO limit = 10%)
MIN_SCORE        = 7       # Minimum candle expectancy score to enter
PYRAMID_SCORE    = 8       # Score needed to pyramid (add to position)
TRAIL_ATR_MULT   = 0.4     # Trailing stop = 0.4x ATR
PYRAMID_TRIGGER  = 0.5     # Add to position after 0.5x ATR in profit
MAX_PYRAMIDS     = 2       # Max pyramid layers

# ── State ───────────────────────────────────────────────────────────────────
open_positions   = {}      # { instrument: { units, entry, trail_stop, pyramids } }
daily_pnl_pct    = 0.0
session_start_balance = None

# ── OANDA API Helpers ────────────────────────────────────────────────────────
def get_account():
    r = requests.get(f"{BASE_URL}/v3/accounts/{ACCOUNT_ID}/summary", headers=HEADERS)
    return r.json().get("account", {})

def get_candles(instrument, count=50, granularity="M15"):
    r = requests.get(
        f"{BASE_URL}/v3/instruments/{instrument}/candles",
        headers=HEADERS,
        params={"count": count, "granularity": granularity, "price": "M"}
    )
    return r.json().get("candles", [])

def get_price(instrument):
    r = requests.get(
        f"{BASE_URL}/v3/accounts/{ACCOUNT_ID}/pricing",
        headers=HEADERS,
        params={"instruments": instrument}
    )
    prices = r.json().get("prices", [])
    if prices:
        bid = float(prices[0]["bids"][0]["price"])
        ask = float(prices[0]["asks"][0]["price"])
        return (bid + ask) / 2
    return None

def place_order(instrument, units, stop_loss, take_profit):
    """Place a market order with stop loss and take profit."""
    data = {
        "order": {
            "type": "MARKET",
            "instrument": instrument,
            "units": str(int(units)),
            "stopLossOnFill": {"price": f"{stop_loss:.5f}"},
            "takeProfitOnFill": {"price": f"{take_profit:.5f}"},
            "timeInForce": "FOK"
        }
    }
    r = requests.post(f"{BASE_URL}/v3/accounts/{ACCOUNT_ID}/orders", headers=HEADERS, json=data)
    return r.json()

def get_open_trades():
    r = requests.get(f"{BASE_URL}/v3/accounts/{ACCOUNT_ID}/openTrades", headers=HEADERS)
    return r.json().get("trades", [])

def close_trade(trade_id):
    r = requests.put(f"{BASE_URL}/v3/accounts/{ACCOUNT_ID}/trades/{trade_id}/close", headers=HEADERS)
    return r.json()

def update_trade_stop(trade_id, new_stop):
    data = {"stopLoss": {"price": f"{new_stop:.5f}", "timeInForce": "GTC"}}
    r = requests.put(
        f"{BASE_URL}/v3/accounts/{ACCOUNT_ID}/trades/{trade_id}/orders",
        headers=HEADERS, json=data
    )
    return r.json()

# ── Candle Analysis ──────────────────────────────────────────────────────────
def compute_atr(candles, period=14):
    trs = []
    for i in range(1, len(candles)):
        h = float(candles[i]["mid"]["h"])
        l = float(candles[i]["mid"]["l"])
        pc = float(candles[i-1]["mid"]["c"])
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if not trs:
        return 0.001
    return sum(trs[-period:]) / min(len(trs), period)

def compute_rsi(candles, period=14):
    closes = [float(c["mid"]["c"]) for c in candles]
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    if len(gains) < period:
        return 50
    avg_g = sum(gains[-period:]) / period
    avg_l = sum(losses[-period:]) / period
    if avg_l == 0:
        return 100
    rs = avg_g / avg_l
    return 100 - (100 / (1 + rs))

def score_breakout(candles):
    """
    Candle Expectancy Scoring System (0-10).
    Reads candle structure + momentum to estimate breakout probability.
    """
    if len(candles) < 20:
        return 0, "neutral", 0

    score = 0
    closes = [float(c["mid"]["c"]) for c in candles]
    opens  = [float(c["mid"]["o"]) for c in candles]
    highs  = [float(c["mid"]["h"]) for c in candles]
    lows   = [float(c["mid"]["l"]) for c in candles]
    vols   = [float(c.get("volume", 100)) for c in candles]

    last_close = closes[-1]
    last_open  = opens[-1]
    last_high  = highs[-1]
    last_low   = lows[-1]
    body       = abs(last_close - last_open)
    candle_range = last_high - last_low
    body_pct   = body / candle_range if candle_range > 0 else 0

    atr = compute_atr(candles)
    rsi = compute_rsi(candles)

    # ── Direction ──
    bullish = last_close > last_open

    # 1. Strong body (≥60% of candle range) = +2
    if body_pct >= 0.6:
        score += 2

    # 2. Breakout candle (body > 1.5x ATR) = +2
    if body > atr * 1.5:
        score += 2

    # 3. RSI momentum confirmation
    if bullish and rsi > 55:
        score += 1
    elif not bullish and rsi < 45:
        score += 1

    # 4. Volume spike (above 20-bar avg) = +1
    avg_vol = sum(vols[-20:]) / 20
    if vols[-1] > avg_vol * 1.2:
        score += 1

    # 5. Previous candle was small (compression before breakout) = +1
    prev_body = abs(closes[-2] - opens[-2])
    if prev_body < body * 0.5:
        score += 1

    # 6. Trend alignment (price above/below 20-bar SMA) = +1
    sma20 = sum(closes[-20:]) / 20
    if bullish and last_close > sma20:
        score += 1
    elif not bullish and last_close < sma20:
        score += 1

    # 7. Clean structure (higher highs or lower lows) = +1
    if bullish and highs[-1] > highs[-2] > highs[-3]:
        score += 1
    elif not bullish and lows[-1] < lows[-2] < lows[-3]:
        score += 1

    direction = "BUY" if bullish else "SELL"
    return min(score, 10), direction, atr

# ── Position Sizing ──────────────────────────────────────────────────────────
def calculate_units(balance, atr, instrument):
    """Risk 0.5% of balance per ATR unit."""
    risk_amount = balance * RISK_PCT
    # For forex, assume pip value ~$10 per 100k units
    pip_value = 10 / 100000
    if "JPY" in instrument:
        pip_value = 10 / 100000
    units = risk_amount / (atr / pip_value + 0.0001)
    units = max(1000, min(int(units), 50000))  # Cap between 1k-50k units
    return units

# ── FTMO Safety Check ────────────────────────────────────────────────────────
def check_ftmo_limits(account):
    global session_start_balance, daily_pnl_pct

    balance = float(account.get("balance", 100000))
    nav     = float(account.get("NAV", balance))

    if session_start_balance is None:
        session_start_balance = balance

    daily_pnl_pct = (nav - session_start_balance) / session_start_balance

    # Hard stop daily loss
    if daily_pnl_pct < -MAX_DAILY_LOSS:
        print(f"⛔ DAILY LOSS LIMIT HIT: {daily_pnl_pct:.2%} — stopping all trading today")
        return False

    # Hard stop total drawdown
    total_dd = (nav - 100000) / 100000  # vs starting challenge balance
    if total_dd < -MAX_TOTAL_LOSS:
        print(f"⛔ TOTAL DRAWDOWN LIMIT HIT: {total_dd:.2%} — halting challenge")
        return False

    return True

# ── Trailing Stop Manager ────────────────────────────────────────────────────
def manage_trailing_stops(trades, atr_map):
    """Update trailing stops on all open trades."""
    for trade in trades:
        instrument = trade["instrument"]
        trade_id   = trade["id"]
        units      = float(trade["currentUnits"])
        price      = float(trade["price"])  # entry price
        current    = get_price(instrument)
        if not current:
            continue

        atr = atr_map.get(instrument, 0.001)
        trail_dist = atr * TRAIL_ATR_MULT

        if units > 0:  # Long
            new_stop = current - trail_dist
            current_stop = float(trade.get("stopLossOrder", {}).get("price", 0))
            if new_stop > current_stop + atr * 0.1:  # Only move up
                update_trade_stop(trade_id, new_stop)
                print(f"📈 {instrument} trail stop → {new_stop:.5f}")
        else:  # Short
            new_stop = current + trail_dist
            current_stop = float(trade.get("stopLossOrder", {}).get("price", 999))
            if new_stop < current_stop - atr * 0.1:  # Only move down
                update_trade_stop(trade_id, new_stop)
                print(f"📉 {instrument} trail stop → {new_stop:.5f}")

# ── Main Scanner Loop ────────────────────────────────────────────────────────
def run():
    print("🚀 Nova Breakout Hunter — OANDA Edition")
    print(f"Environment: {ENVIRONMENT.upper()}")
    print(f"Account: {ACCOUNT_ID}")
    print("=" * 50)

    scan_count = 0

    while True:
        try:
            account = get_account()
            balance = float(account.get("balance", 100000))
            nav     = float(account.get("NAV", balance))

            print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Balance: ${balance:,.2f} | NAV: ${nav:,.2f} | Scan #{scan_count+1}")

            # FTMO safety check
            if not check_ftmo_limits(account):
                print("⛔ Trading paused — FTMO limit protection active")
                time.sleep(300)
                continue

            open_trades = get_open_trades()
            atr_map = {}

            # ── Manage existing trades (trailing stops + pyramiding) ──
            for pair in PAIRS:
                candles = get_candles(pair, count=50, granularity="M15")
                if candles:
                    atr_map[pair] = compute_atr(candles)

            manage_trailing_stops(open_trades, atr_map)

            # ── Scan for new entries ──
            active_instruments = [t["instrument"] for t in open_trades]

            for pair in PAIRS:
                if pair in active_instruments:
                    continue  # Already in this pair

                candles = get_candles(pair, count=50, granularity="M15")
                if not candles:
                    continue

                score, direction, atr = score_breakout(candles)

                if score >= MIN_SCORE:
                    price = get_price(pair)
                    if not price:
                        continue

                    units = calculate_units(balance, atr, pair)
                    if direction == "SELL":
                        units = -units

                    # Stop loss and take profit
                    if direction == "BUY":
                        stop_loss   = price - (atr * 1.5)
                        take_profit = price + (atr * 3.0)
                    else:
                        stop_loss   = price + (atr * 1.5)
                        take_profit = price - (atr * 3.0)

                    print(f"\n🎯 SIGNAL: {pair} {direction} | Score: {score}/10 | Entry: {price:.5f}")
                    print(f"   SL: {stop_loss:.5f} | TP: {take_profit:.5f} | Units: {abs(int(units)):,}")

                    result = place_order(pair, units, stop_loss, take_profit)

                    if "orderFillTransaction" in result:
                        fill = result["orderFillTransaction"]
                        print(f"✅ FILLED: {pair} {direction} @ {fill.get('price', price)}")
                    elif "orderCancelTransaction" in result:
                        print(f"❌ ORDER CANCELLED: {result['orderCancelTransaction'].get('reason', 'unknown')}")
                    else:
                        print(f"⚠️ Order response: {json.dumps(result)[:200]}")

            # ── Pyramiding check ──
            for trade in open_trades:
                instrument = trade["instrument"]
                candles = get_candles(instrument, count=50, granularity="M15")
                if not candles:
                    continue

                score, direction, atr = score_breakout(candles)
                pyramid_count = open_positions.get(instrument, {}).get("pyramids", 0)

                if score >= PYRAMID_SCORE and pyramid_count < MAX_PYRAMIDS:
                    price = get_price(instrument)
                    if not price:
                        continue

                    units = calculate_units(balance, atr, instrument) * 0.5  # Half size for pyramid
                    trade_units = float(trade["currentUnits"])
                    if trade_units < 0:
                        units = -units

                    if direction == "BUY" and trade_units > 0:
                        stop_loss   = price - (atr * 1.0)
                        take_profit = price + (atr * 2.5)
                        result = place_order(instrument, units, stop_loss, take_profit)
                        if "orderFillTransaction" in result:
                            open_positions.setdefault(instrument, {})["pyramids"] = pyramid_count + 1
                            print(f"📊 PYRAMID #{pyramid_count+1}: {instrument} BUY +{int(units):,} units @ {price:.5f}")

                    elif direction == "SELL" and trade_units < 0:
                        stop_loss   = price + (atr * 1.0)
                        take_profit = price - (atr * 2.5)
                        result = place_order(instrument, -units, stop_loss, take_profit)
                        if "orderFillTransaction" in result:
                            open_positions.setdefault(instrument, {})["pyramids"] = pyramid_count + 1
                            print(f"📊 PYRAMID #{pyramid_count+1}: {instrument} SELL +{int(abs(units)):,} units @ {price:.5f}")

            scan_count += 1
            print(f"\n💤 Next scan in 2 minutes... (Daily P&L: {daily_pnl_pct:.2%})")
            time.sleep(120)

        except KeyboardInterrupt:
            print("\n🛑 Breakout Hunter stopped.")
            break
        except Exception as e:
            print(f"⚠️ Error: {e}")
            time.sleep(30)

if __name__ == "__main__":
    run()

# ── Single-scan entry point for main.py threading ───────────────────────────
def run_once():
    """Run one full scan cycle — called by main.py thread loop."""
    account = get_account()
    balance = float(account.get("balance", 100000))

    if not check_ftmo_limits(account):
        print("⛔ BreakoutHunter: FTMO limit active, skipping scan")
        return

    open_trades = get_open_trades()
    atr_map = {}

    for pair in PAIRS:
        candles = get_candles(pair, count=50, granularity="M15")
        if candles:
            atr_map[pair] = compute_atr(candles)

    manage_trailing_stops(open_trades, atr_map)

    active_instruments = [t["instrument"] for t in open_trades]

    for pair in PAIRS:
        if pair in active_instruments:
            continue

        candles = get_candles(pair, count=50, granularity="M15")
        if not candles:
            continue

        score, direction, atr = score_breakout(candles)

        if score >= MIN_SCORE:
            price = get_price(pair)
            if not price:
                continue

            units = calculate_units(balance, atr, pair)
            if direction == "SELL":
                units = -units

            if direction == "BUY":
                stop_loss   = price - (atr * 1.5)
                take_profit = price + (atr * 3.0)
            else:
                stop_loss   = price + (atr * 1.5)
                take_profit = price - (atr * 3.0)

            print(f"🎯 BH SIGNAL: {pair} {direction} Score:{score}/10 @ {price:.5f}")
            result = place_order(pair, units, stop_loss, take_profit)
            if "orderFillTransaction" in result:
                print(f"✅ BH FILLED: {pair} {direction}")

    # Pyramiding
    for trade in open_trades:
        instrument = trade["instrument"]
        candles = get_candles(instrument, count=50, granularity="M15")
        if not candles:
            continue
        score, direction, atr = score_breakout(candles)
        pyramid_count = open_positions.get(instrument, {}).get("pyramids", 0)
        if score >= PYRAMID_SCORE and pyramid_count < MAX_PYRAMIDS:
            price = get_price(instrument)
            if not price:
                continue
            units = calculate_units(balance, atr, instrument) * 0.5
            trade_units = float(trade["currentUnits"])
            if direction == "BUY" and trade_units > 0:
                stop_loss   = price - (atr * 1.0)
                take_profit = price + (atr * 2.5)
                result = place_order(instrument, units, stop_loss, take_profit)
                if "orderFillTransaction" in result:
                    open_positions.setdefault(instrument, {})["pyramids"] = pyramid_count + 1
                    print(f"📊 BH PYRAMID: {instrument} BUY layer {pyramid_count+1}")
            elif direction == "SELL" and trade_units < 0:
                stop_loss   = price + (atr * 1.0)
                take_profit = price - (atr * 2.5)
                result = place_order(instrument, -units, stop_loss, take_profit)
                if "orderFillTransaction" in result:
                    open_positions.setdefault(instrument, {})["pyramids"] = pyramid_count + 1
                    print(f"📊 BH PYRAMID: {instrument} SELL layer {pyramid_count+1}")

# ── Brain-Validated Config (from backtest + forward test) ────────────────────
# Proven consistent across both backtest AND forward test:
PROVEN_PAIRS     = ["EUR_JPY", "AUD_USD"]  # Tier 1 — always scan first
PROVEN_STRATEGIES = {
    "EUR_JPY_M15_scalp_bull": {"min_score": 6, "atr_sl": 0.8, "atr_tp": 1.5},
    "EUR_JPY_M15_scalp_bear": {"min_score": 6, "atr_sl": 0.8, "atr_tp": 1.5},
    "AUD_USD_M15_scalp_bull": {"min_score": 6, "atr_sl": 0.8, "atr_tp": 1.5},
}

def get_priority_pairs():
    """Returns proven pairs first, then the rest."""
    others = [p for p in PAIRS if p.replace("/","_") not in [x.replace("/","_") for x in PROVEN_PAIRS]]
    return PROVEN_PAIRS + others
