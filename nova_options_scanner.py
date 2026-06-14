#!/usr/bin/env python3
"""
Nova Options Scanner
Scans AAPL, NVDA, SPY, QQQ for high-probability options setups.
Uses same candle expectancy scoring as Breakout Hunter.
Targets: momentum plays, earnings moves, breakout entries.
Broker: Tradier API (paper trading first)
"""

import os, json, requests, time
from datetime import datetime, timezone, timedelta

TRADIER_TOKEN = os.environ.get("TRADIER_TOKEN", "")
TRADIER_BASE  = "https://sandbox.tradier.com/v1"  # paper trading
HEADERS       = {"Authorization": f"Bearer {TRADIER_TOKEN}", "Accept": "application/json"}

TICKERS = ["AAPL", "NVDA", "SPY", "QQQ", "TSLA", "MSFT", "AMZN"]

# Options strategy config
SETUPS = {
    "momentum_call": {"min_score": 7, "dte": 7,  "delta": 0.40, "type": "call"},
    "momentum_put":  {"min_score": 7, "dte": 7,  "delta": 0.40, "type": "put"},
    "breakout_call": {"min_score": 8, "dte": 14, "delta": 0.35, "type": "call"},
    "breakout_put":  {"min_score": 8, "dte": 14, "delta": 0.35, "type": "put"},
    "scalp_call":    {"min_score": 7, "dte": 3,  "delta": 0.45, "type": "call"},
    "scalp_put":     {"min_score": 7, "dte": 3,  "delta": 0.45, "type": "put"},
}

# ── Fetch stock data (Alpha Vantage fallback) ─────────────────────────────────
def get_stock_bars(ticker, interval="5min", outputsize="compact"):
    key = os.environ.get("ALPHA_VANTAGE_API_KEY", "")
    url = (
        f"https://www.alphavantage.co/query"
        f"?function=TIME_SERIES_INTRADAY&symbol={ticker}"
        f"&interval={interval}&outputsize={outputsize}&apikey={key}"
    )
    try:
        r = requests.get(url, timeout=10)
        data = r.json()
        series_key = f"Time Series ({interval})"
        series = data.get(series_key, {})
        bars = []
        for ts in sorted(series.keys())[-60:]:
            b = series[ts]
            bars.append({
                "time": ts,
                "open":  float(b["1. open"]),
                "high":  float(b["2. high"]),
                "low":   float(b["3. low"]),
                "close": float(b["4. close"]),
                "vol":   float(b["5. volume"])
            })
        return bars
    except:
        return []

# ── Score stock setup (mirrors forex scorer) ─────────────────────────────────
def score_stock(bars, direction="call"):
    if len(bars) < 20:
        return 0

    closes = [b["close"] for b in bars]
    opens  = [b["open"]  for b in bars]
    highs  = [b["high"]  for b in bars]
    lows   = [b["low"]   for b in bars]
    vols   = [b["vol"]   for b in bars]

    c, o, h, l = closes[-1], opens[-1], highs[-1], lows[-1]
    body = abs(c - o); rng = h - l
    body_pct = body / rng if rng else 0
    bullish = c > o

    # ATR
    trs = [max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
           for i in range(1, len(bars))]
    atr_val = sum(trs[-14:]) / 14 if len(trs) >= 14 else 1.0

    # RSI
    gains = [max(closes[i]-closes[i-1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i-1]-closes[i], 0) for i in range(1, len(closes))]
    rsi_val = 50
    if len(gains) >= 14:
        ag = sum(gains[-14:])/14; al = sum(losses[-14:])/14
        rsi_val = 100 - (100/(1+(ag/al))) if al else 100

    sma20 = sum(closes[-20:]) / 20
    avg_vol = sum(vols[-20:]) / 20

    score = 0

    if direction == "call":
        if bullish: score += 1
        if body_pct > 0.6: score += 2
        if body > atr_val * 1.2: score += 1
        if rsi_val > 55 and rsi_val < 78: score += 2  # momentum not overbought
        if vols[-1] > avg_vol * 1.3: score += 2  # volume surge critical for stocks
        if c > sma20: score += 1
        if highs[-1] > max(highs[-10:-1]): score += 1  # new high breakout
    else:
        if not bullish: score += 1
        if body_pct > 0.6: score += 2
        if body > atr_val * 1.2: score += 1
        if rsi_val < 45 and rsi_val > 22: score += 2
        if vols[-1] > avg_vol * 1.3: score += 2
        if c < sma20: score += 1
        if lows[-1] < min(lows[-10:-1]): score += 1  # new low breakdown

    return min(score, 10)

# ── Session check (US market hours) ──────────────────────────────────────────
def is_market_open():
    ET = timezone(timedelta(hours=-4))
    now = datetime.now(ET)
    if now.weekday() >= 5: return False  # Weekend
    return (now.hour == 9 and now.minute >= 30) or (10 <= now.hour <= 15)

# ── Main scanner ──────────────────────────────────────────────────────────────
def scan_once():
    if not is_market_open():
        print(f"[OptionsScanner] Market closed — skipping scan")
        return []

    signals = []
    print(f"[{datetime.now().strftime('%H:%M')}] Options Scanner scanning {len(TICKERS)} tickers...")

    for ticker in TICKERS:
        bars = get_stock_bars(ticker)
        if not bars:
            continue

        for setup_name, cfg in SETUPS.items():
            direction = cfg["type"]
            score = score_stock(bars, direction)

            if score >= cfg["min_score"]:
                price = bars[-1]["close"]
                signal = {
                    "ticker": ticker,
                    "setup": setup_name,
                    "score": score,
                    "direction": direction.upper(),
                    "price": price,
                    "dte": cfg["dte"],
                    "target_delta": cfg["delta"],
                    "time": datetime.now(timezone.utc).isoformat()
                }
                signals.append(signal)
                print(f"  🎯 OPTIONS SIGNAL: {ticker} {direction.upper()} | Score:{score}/10 | DTE:{cfg['dte']} | Price:${price:.2f}")

    if not signals:
        print("  No signals this scan")

    return signals

def run():
    print("📈 Nova Options Scanner — Live")
    print(f"Tracking: {', '.join(TICKERS)}")
    print("=" * 50)
    while True:
        try:
            scan_once()
        except Exception as e:
            print(f"[OptionsScanner ERROR] {e}")
        time.sleep(300)  # Every 5 min during market hours

if __name__ == "__main__":
    run()
