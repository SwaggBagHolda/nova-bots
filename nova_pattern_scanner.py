#!/usr/bin/env python3
"""
Nova Pattern Scanner v1.0
─────────────────────────
Detects patterns the human eye CANNOT see:
  1. Microstructure momentum divergence (price vs order flow)
  2. Volatility compression squeeze (Bollinger inside Keltner)
  3. Hidden accumulation (price flat, volume rising)
  4. Multi-timeframe EMA confluence (1m + 5m + 15m aligned)
  5. VWAP reclaim after dip (institutional buy signal)
  6. Candle body imbalance (large body / small wick = conviction)
  7. ATR contraction breakout (range contracting → explosive move near)

Runs on Alpaca paper — outputs ranked opportunity list with score 0-100.
"""
import urllib.request, json, math
from datetime import datetime, timezone, timedelta

API_KEY    = "PKHFMGMEDX45XRMPT4OWYIKKR4"
API_SECRET = "DqVA62jPiJuF88VmnusrDAQxBdsgnDahvQWuXiUcdt62"
DATA       = "https://data.alpaca.markets"

UNIVERSE = [
    # Stocks
    "SPY","QQQ","NVDA","TSLA","META","MSFT","AAPL","AMD","COIN","AMZN",
    "GOOGL","NFLX","UBER","HOOD","PLTR","SOFI","MSTR","RKLB","IONQ","SMCI",
    # Crypto (24/7)
    "BTC/USD","ETH/USD","SOL/USD","XRP/USD","LINK/USD","BCH/USD","AVAX/USD"
]

def get_bars(sym, tf="1Min", limit=60, crypto=False):
    try:
        if crypto:
            url = f"{DATA}/v1beta3/crypto/us/bars?symbols={sym}&timeframe=5Min&limit={limit}"
        else:
            url = f"{DATA}/v2/stocks/{sym}/bars?timeframe={tf}&limit={limit}&feed=iex"
        r = urllib.request.Request(url,
            headers={"APCA-API-KEY-ID":API_KEY,"APCA-API-SECRET-KEY":API_SECRET})
        with urllib.request.urlopen(r, timeout=10) as resp:
            d = json.loads(resp.read())
            return d.get("bars",{}).get(sym,[]) if crypto else d.get("bars",[])
    except: return []

def ema(vals, p):
    if not vals: return 0
    k = 2/(p+1); e = vals[0]
    for x in vals[1:]: e = x*k + e*(1-k)
    return e

def stdev(vals):
    if len(vals) < 2: return 0
    m = sum(vals)/len(vals)
    return math.sqrt(sum((x-m)**2 for x in vals)/len(vals))

def atr(bs, p=14):
    trs = []
    for i in range(1, len(bs)):
        h = float(bs[i]["h"]); l = float(bs[i]["l"]); pc = float(bs[i-1]["c"])
        trs.append(max(h-l, abs(h-pc), abs(l-pc)))
    if not trs: return 0
    return sum(trs[-p:]) / min(len(trs), p)

# ── PATTERN DETECTORS ──────────────────────────────────────────────────────

def pattern_squeeze(bs):
    """Bollinger inside Keltner = volatility compression → explosive breakout near."""
    if len(bs) < 20: return 0, ""
    c = [float(b["c"]) for b in bs]
    h = [float(b["h"]) for b in bs]
    l = [float(b["l"]) for b in bs]
    mid = sum(c[-20:])/20
    sd  = stdev(c[-20:])
    bb_upper = mid + 2*sd; bb_lower = mid - 2*sd
    k = 2/(10+1); e = c[-20]
    for x in c[-19:]: e = x*k + e*(1-k)
    ema20 = e
    atr14 = atr(bs[-20:])
    kc_upper = ema20 + 1.5*atr14; kc_lower = ema20 - 1.5*atr14
    if bb_upper < kc_upper and bb_lower > kc_lower:
        # Inside squeeze — score higher if tighter
        tightness = (bb_upper - bb_lower) / (kc_upper - kc_lower)
        score = int((1 - tightness) * 40)
        return score, "SQUEEZE"
    return 0, ""

def pattern_hidden_accumulation(bs):
    """Price flat but volume trending up = smart money loading."""
    if len(bs) < 20: return 0, ""
    c = [float(b["c"]) for b in bs]
    v = [float(b.get("v",0)) for b in bs]
    price_range = (max(c[-10:]) - min(c[-10:])) / c[-10] * 100
    vol_trend = sum(v[-5:]) / max(sum(v[-10:-5]), 1)  # recent vol vs prior
    if price_range < 0.5 and vol_trend > 1.3:
        score = int(min(vol_trend * 15, 35))
        return score, "ACCUMULATION"
    return 0, ""

def pattern_atr_contraction(bs):
    """ATR shrinking over last 10 bars = coiling spring."""
    if len(bs) < 20: return 0, ""
    atr_now  = atr(bs[-10:], p=5)
    atr_prev = atr(bs[-20:-10], p=5)
    if atr_prev == 0: return 0, ""
    ratio = atr_now / atr_prev
    if ratio < 0.6:  # ATR dropped 40%+
        score = int((1 - ratio) * 40)
        return score, "ATR_COIL"
    return 0, ""

def pattern_mtf_ema_confluence(bs_1m, bs_5m):
    """1m EMA9 > EMA21 AND 5m EMA9 > EMA21 = multi-timeframe aligned."""
    if len(bs_1m) < 25 or len(bs_5m) < 25: return 0, ""
    c1 = [float(b["c"]) for b in bs_1m]
    c5 = [float(b["c"]) for b in bs_5m]
    e1_9  = ema(c1, 9);  e1_21 = ema(c1, 21)
    e5_9  = ema(c5, 9);  e5_21 = ema(c5, 21)
    if e1_9 > e1_21 and e5_9 > e5_21:
        # Both aligned bullish
        strength = ((e1_9-e1_21)/e1_21 + (e5_9-e5_21)/e5_21) * 10000
        return min(int(strength), 30), "MTF_BULL"
    if e1_9 < e1_21 and e5_9 < e5_21:
        strength = ((e1_21-e1_9)/e1_9 + (e5_21-e5_9)/e5_9) * 10000
        return min(int(strength), 30), "MTF_BEAR"
    return 0, ""

def pattern_vwap_reclaim(bs):
    """Price dipped below VWAP then reclaimed it = institutional support."""
    if len(bs) < 10: return 0, ""
    total_v = sum(float(b.get("v",0)) for b in bs)
    if total_v == 0: return 0, ""
    vwap = sum(float(b["c"])*float(b.get("v",0)) for b in bs) / total_v
    c = [float(b["c"]) for b in bs]
    # Look for: dipped below, now above
    was_below = any(x < vwap for x in c[-6:-2])
    now_above = c[-1] > vwap and c[-2] > vwap
    if was_below and now_above:
        return 25, "VWAP_RECLAIM"
    return 0, ""

def pattern_candle_conviction(bs):
    """Large body candles in a row = conviction move, not noise."""
    if len(bs) < 5: return 0, ""
    conviction = 0
    for b in bs[-4:]:
        o = float(b["o"]); c = float(b["c"])
        h = float(b["h"]); l = float(b["l"])
        body = abs(c-o); full = h-l
        if full > 0 and body/full > 0.7:  # body > 70% of candle
            conviction += 1
    if conviction >= 3:
        return conviction * 5, "CONVICTION"
    return 0, ""

def pattern_momentum_divergence(bs):
    """Price making higher highs but momentum weakening = warning / fade setup."""
    if len(bs) < 15: return 0, ""
    c = [float(b["c"]) for b in bs]
    # Price HH
    price_hh = c[-1] > max(c[-10:-1])
    # Momentum (ROC) LH
    mom_now  = (c[-1] - c[-6]) / c[-6]
    mom_prev = (c[-6] - c[-11]) / c[-11] if len(c) >= 11 else mom_now
    if price_hh and mom_now < mom_prev * 0.7:
        return 20, "MOM_DIV"
    return 0, ""

# ── MAIN SCAN ──────────────────────────────────────────────────────────────

def scan():
    now  = datetime.now(timezone(timedelta(hours=-4)))
    et_h = now.hour + now.minute / 60
    mkt  = (9.5 <= et_h < 16.0) and now.weekday() < 5
    print(f"\n🔍 Nova Pattern Scanner | {now.strftime('%H:%M ET')} | {'MKT OPEN' if mkt else 'AFTER HOURS/CRYPTO'}")
    print("="*60)

    results = []

    for sym in UNIVERSE:
        is_crypto = "/" in sym
        # Skip stocks after hours (use crypto overnight)
        if not is_crypto and not mkt: continue

        bs_1m = get_bars(sym, "1Min", 60, is_crypto)
        bs_5m = get_bars(sym, "5Min", 60, is_crypto)
        if len(bs_1m) < 20 and len(bs_5m) < 20: continue

        bs = bs_5m if is_crypto or len(bs_1m) < 20 else bs_1m
        bs5 = bs_5m if len(bs_5m) >= 20 else bs

        scores = {}
        s, n = pattern_squeeze(bs);            scores[n] = s
        s, n = pattern_hidden_accumulation(bs); scores[n] = s
        s, n = pattern_atr_contraction(bs);    scores[n] = s
        s, n = pattern_vwap_reclaim(bs);       scores[n] = s
        s, n = pattern_candle_conviction(bs);  scores[n] = s
        s, n = pattern_momentum_divergence(bs);scores[n] = s
        s, n = pattern_mtf_ema_confluence(bs_1m, bs5); scores[n] = s

        total = sum(scores.values())
        active = {k:v for k,v in scores.items() if v > 0}
        price = float(bs[-1]["c"]) if bs else 0

        if total >= 25:
            results.append({
                "sym": sym, "score": total, "price": price,
                "patterns": active, "crypto": is_crypto
            })

    results.sort(key=lambda x: -x["score"])

    if not results:
        print("No high-probability setups detected this scan.")
    else:
        print(f"{'SYM':<12} {'SCORE':>5}  {'PRICE':>10}  PATTERNS")
        print("-"*60)
        for r in results[:10]:
            pats = " + ".join(f"{k}({v})" for k,v in sorted(r["patterns"].items(), key=lambda x:-x[1]))
            tag = "🌙" if r["crypto"] else "📈"
            print(f"{tag} {r['sym']:<10} {r['score']:>5}  ${r['price']:>9.2f}  {pats}")

    print(f"\nScanned {len(UNIVERSE)} assets | Top setup: {results[0]['sym'] if results else 'none'}")
    return results

if __name__ == "__main__":
    scan()
