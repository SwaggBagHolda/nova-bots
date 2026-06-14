#!/usr/bin/env python3
"""
Nova Candle Prediction Engine v1
═══════════════════════════════════
Pattern-matching + feature-based candle predictor.
Learns from every closed trade. Scores setups 0-100 before entry.
Only recommends entry if confidence >= threshold (default 65%).

How it works:
1. Extracts features from current candle + context (RSI, ADX, vol, candle shape, time)
2. Looks back through trade history for similar setups
3. Calculates win rate of those similar setups = confidence score
4. Returns: {"score": 72, "confidence": "HIGH", "enter": True, "reason": "..."}

Plugs into any bot:
    from nova_candle_predictor import predict
    signal = predict(bars, side="long", trade_history=history)
    if not signal["enter"]: continue
"""

import json, os, math
from datetime import datetime, timezone, timedelta

HISTORY_FILE = "/tmp/candle_predictor_history.json"
MIN_CONFIDENCE = 65      # minimum score to allow entry
MIN_SAMPLE     = 5       # need at least 5 similar trades to trust the score
SIMILARITY_THRESHOLD = 0.75  # how close features need to be to count as "similar"

# ── FEATURE EXTRACTION ─────────────────────────────────────────────────────
def extract_features(bars, side="long"):
    """Extract normalized features from bar data."""
    if len(bars) < 20:
        return None

    closes = [b['c'] for b in bars]
    highs  = [b['h'] for b in bars]
    lows   = [b['l'] for b in bars]
    vols   = [b['v'] for b in bars]

    # RSI
    rsi_val = _rsi(closes)

    # ADX
    adx_val = _adx(bars)

    # Bollinger Band position (where is price in the band?)
    bb_pos = _bb_position(closes)

    # Candle shape features
    last = bars[-1]
    body = abs(last['c'] - last['o'])
    total_range = last['h'] - last['l'] if last['h'] != last['l'] else 0.0001
    body_ratio = body / total_range  # 1.0 = full body, 0.0 = doji

    upper_wick = last['h'] - max(last['c'], last['o'])
    lower_wick = min(last['c'], last['o']) - last['l']
    wick_ratio = (upper_wick - lower_wick) / total_range  # pos = upper wick heavy, neg = lower wick heavy

    # Volume ratio vs 20-bar average
    avg_vol = sum(vols[-21:-1]) / 20 if len(vols) >= 21 else vols[-1]
    vol_ratio = vols[-1] / avg_vol if avg_vol > 0 else 1.0

    # Time of day (ET hour, normalized 0-1)
    ET = timezone(timedelta(hours=-4))
    hour = datetime.now(ET).hour
    time_norm = hour / 23.0

    # Trend direction (EMA21 vs EMA50)
    e21 = _ema(closes[-40:], 21)
    e50 = _ema(closes[-70:], 50) if len(closes) >= 70 else _ema(closes, min(len(closes), 50))
    trend = 1.0 if e21 > e50 else 0.0

    # Momentum: last 3 candles direction
    momentum = sum(1 if closes[-(i+1)] > closes[-(i+2)] else -1 for i in range(3)) / 3.0

    # Side encoding
    side_enc = 1.0 if side == "long" else 0.0

    return {
        "rsi":        round(rsi_val, 2),
        "adx":        round(adx_val, 2),
        "bb_pos":     round(bb_pos, 3),
        "body_ratio": round(body_ratio, 3),
        "wick_ratio": round(wick_ratio, 3),
        "vol_ratio":  round(min(vol_ratio, 5.0), 3),
        "time_norm":  round(time_norm, 3),
        "trend":      trend,
        "momentum":   round(momentum, 3),
        "side":       side_enc,
    }

# ── SIMILARITY ─────────────────────────────────────────────────────────────
def _similarity(f1, f2):
    """Cosine-like similarity between two feature dicts. Returns 0-1."""
    keys = ["rsi", "adx", "bb_pos", "body_ratio", "wick_ratio", "vol_ratio", "time_norm", "trend", "momentum"]
    # Normalize ranges for each feature
    ranges = {"rsi": 100, "adx": 100, "bb_pos": 1, "body_ratio": 1,
              "wick_ratio": 2, "vol_ratio": 5, "time_norm": 1, "trend": 1, "momentum": 2}
    diffs = []
    for k in keys:
        r = ranges.get(k, 1)
        diff = abs(f1.get(k, 0) - f2.get(k, 0)) / r
        diffs.append(diff)
    avg_diff = sum(diffs) / len(diffs)
    return 1.0 - avg_diff  # higher = more similar

# ── HISTORY MANAGEMENT ─────────────────────────────────────────────────────
def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            return json.load(open(HISTORY_FILE))
        except:
            pass
    return []

def save_history(history):
    # Keep last 500 trades
    history = history[-500:]
    json.dump(history, open(HISTORY_FILE, "w"), indent=2)

def record_trade_result(bars, side, result, pnl_pct, symbol=""):
    """Call this after every closed trade to train the predictor."""
    features = extract_features(bars, side)
    if features is None:
        return
    history = load_history()
    history.append({
        "symbol":   symbol,
        "side":     side,
        "features": features,
        "result":   result,   # "WIN" or "LOSS"
        "pnl_pct":  pnl_pct,
        "logged_at": datetime.now(timezone.utc).isoformat()
    })
    save_history(history)

# ── MAIN PREDICTION ────────────────────────────────────────────────────────
def predict(bars, side="long", symbol="", min_confidence=MIN_CONFIDENCE):
    """
    Main entry point. Call before placing any trade.

    Returns dict:
        {
            "score": 0-100,
            "confidence": "HIGH" / "MEDIUM" / "LOW" / "INSUFFICIENT_DATA",
            "enter": True/False,
            "similar_trades": N,
            "win_rate": 0.0-1.0,
            "reason": "human readable explanation",
            "features": {...}
        }
    """
    features = extract_features(bars, side)
    if features is None:
        return {
            "score": 50, "confidence": "INSUFFICIENT_DATA",
            "enter": True,  # not enough bars, don't block
            "similar_trades": 0, "win_rate": 0.5,
            "reason": "Not enough bar data for prediction",
            "features": {}
        }

    history = load_history()

    if len(history) < MIN_SAMPLE:
        # Not enough history yet — allow trade but flag it
        return {
            "score": 50, "confidence": "INSUFFICIENT_DATA",
            "enter": True,
            "similar_trades": len(history), "win_rate": 0.5,
            "reason": f"Building history ({len(history)}/{MIN_SAMPLE} trades logged). Allowing entry.",
            "features": features
        }

    # Find similar historical setups
    similar = []
    for record in history:
        sim = _similarity(features, record["features"])
        if sim >= SIMILARITY_THRESHOLD:
            similar.append((sim, record))

    if len(similar) < MIN_SAMPLE:
        return {
            "score": 50, "confidence": "INSUFFICIENT_DATA",
            "enter": True,
            "similar_trades": len(similar), "win_rate": 0.5,
            "reason": f"Only {len(similar)} similar setups found (need {MIN_SAMPLE}). Allowing entry.",
            "features": features
        }

    # Weight wins by similarity score
    total_weight = sum(s for s, _ in similar)
    win_weight   = sum(s for s, r in similar if r["result"] == "WIN")
    win_rate     = win_weight / total_weight if total_weight > 0 else 0.5

    # Avg PnL of similar winning trades
    win_pnls = [r["pnl_pct"] for _, r in similar if r["result"] == "WIN"]
    avg_win_pnl = sum(win_pnls) / len(win_pnls) if win_pnls else 0

    # Score = win rate * 100, adjusted for sample size confidence
    sample_factor = min(1.0, len(similar) / 20)  # full confidence at 20+ samples
    score = int(win_rate * 100 * (0.7 + 0.3 * sample_factor))

    # Confidence label
    if score >= 75:
        confidence = "HIGH"
    elif score >= min_confidence:
        confidence = "MEDIUM"
    else:
        confidence = "LOW"

    enter = score >= min_confidence

    # Build reason string
    top_features = _explain_features(features, side)
    reason = (
        f"Score {score}/100 | WR {win_rate*100:.0f}% on {len(similar)} similar trades | "
        f"Avg win: {avg_win_pnl:+.2f}% | {top_features}"
    )

    return {
        "score":          score,
        "confidence":     confidence,
        "enter":          enter,
        "similar_trades": len(similar),
        "win_rate":       round(win_rate, 3),
        "avg_win_pnl":    round(avg_win_pnl, 3),
        "reason":         reason,
        "features":       features
    }

# ── FEATURE EXPLAINER ──────────────────────────────────────────────────────
def _explain_features(f, side):
    parts = []
    rsi = f["rsi"]
    adx = f["adx"]
    vol = f["vol_ratio"]
    bb  = f["bb_pos"]
    trend = f["trend"]

    parts.append(f"RSI={rsi:.0f}")
    parts.append(f"ADX={adx:.0f}")
    parts.append(f"Vol={vol:.1f}x")

    if side == "long":
        if bb < 0.2:
            parts.append("near-BB-low✓")
        elif bb > 0.8:
            parts.append("near-BB-high⚠")
    else:
        if bb > 0.8:
            parts.append("near-BB-high✓")
        elif bb < 0.2:
            parts.append("near-BB-low⚠")

    if trend == 1.0 and side == "long":
        parts.append("trend-aligned✓")
    elif trend == 0.0 and side == "short":
        parts.append("trend-aligned✓")
    else:
        parts.append("counter-trend⚠")

    return " | ".join(parts)

# ── INDICATORS ─────────────────────────────────────────────────────────────
def _ema(closes, p):
    if not closes or len(closes) < 2: return closes[-1] if closes else 0
    k = 2 / (p + 1); e = closes[0]
    for c in closes[1:]: e = c * k + e * (1 - k)
    return e

def _rsi(closes, p=14):
    if len(closes) < p + 1: return 50.0
    gains = [max(closes[i] - closes[i-1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i-1] - closes[i], 0) for i in range(1, len(closes))]
    ag = sum(gains[-p:]) / p
    al = sum(losses[-p:]) / p
    return 100 - 100 / (1 + ag / al) if al > 0 else 100.0

def _adx(bars, p=14):
    if len(bars) < p * 2: return 15.0
    pd_, md_, tr_ = [], [], []
    for i in range(1, len(bars)):
        h, l = bars[i]['h'], bars[i]['l']
        ph, pl, pc = bars[i-1]['h'], bars[i-1]['l'], bars[i-1]['c']
        pd_.append(max(h - ph, 0) if (h - ph) > (pl - l) else 0)
        md_.append(max(pl - l, 0) if (pl - l) > (h - ph) else 0)
        tr_.append(max(h - l, abs(h - pc), abs(l - pc)))
    a = sum(tr_[-p:]) / p
    if a == 0: return 0
    pi = (sum(pd_[-p:]) / p) / a * 100
    mi = (sum(md_[-p:]) / p) / a * 100
    return abs(pi - mi) / (pi + mi + 1e-9) * 100

def _bb_position(closes, p=20):
    """Where is price in the Bollinger Band? 0 = at lower band, 1 = at upper band."""
    if len(closes) < p: return 0.5
    w = closes[-p:]
    mid = sum(w) / p
    sd = math.sqrt(sum((x - mid) ** 2 for x in w) / p)
    upper = mid + 2 * sd
    lower = mid - 2 * sd
    rng = upper - lower
    if rng == 0: return 0.5
    return max(0.0, min(1.0, (closes[-1] - lower) / rng))

# ── STATS REPORTER ─────────────────────────────────────────────────────────
def get_stats():
    """Returns summary of predictor performance."""
    history = load_history()
    if not history:
        return {"total": 0, "message": "No trade history yet."}

    total = len(history)
    wins  = sum(1 for r in history if r["result"] == "WIN")
    wr    = wins / total * 100 if total > 0 else 0

    # Best time of day
    hour_wins = {}
    hour_total = {}
    for r in history:
        ET = timezone(timedelta(hours=-4))
        try:
            ts = datetime.fromisoformat(r["logged_at"].replace("Z", "+00:00"))
            h = ts.astimezone(ET).hour
        except:
            h = 12
        hour_total[h] = hour_total.get(h, 0) + 1
        if r["result"] == "WIN":
            hour_wins[h] = hour_wins.get(h, 0) + 1

    best_hour = max(hour_total, key=lambda h: hour_wins.get(h, 0) / hour_total[h]) if hour_total else None

    return {
        "total_trades": total,
        "win_rate": round(wr, 1),
        "best_hour_ET": best_hour,
        "history_size": total,
        "message": f"{total} trades logged | {wr:.0f}% WR | Best hour: {best_hour}:00 ET"
    }

if __name__ == "__main__":
    print("Nova Candle Predictor — Test Run")
    stats = get_stats()
    print(stats["message"])
