import nova_logger
#!/usr/bin/env python3
"""
Nova Paper Trader — Live Paper Money Engine
Uses REAL CoinGecko prices every 2 min.
Brain-scored entries only (score >= 62).
Tracks open positions, closes at TP/SL/timeout.
Every closed trade logged to Base44 DB.
$1,000 per position | Max 3 open | 48-bar timeout
"""
import os, json, subprocess, time, urllib.request
from datetime import datetime, timezone

HISTORY_FILE  = "/tmp/candle_predictor_history.json"
STATE_FILE    = "/tmp/paper_portfolio.json"
BASE44_URL    = "https://app.base44.com/api/apps/69bf82ce8c526c379bdab3ce/entities/ScalperTrade"
TOKEN         = os.environ.get("BASE44_SERVICE_TOKEN", "")
BASE_RISK_PCT = 0.08      # 8% of equity per trade (compounding)
MIN_SCORE     = 72        # brain score gate
SL_PCT        = 0.006     # 0.6% stop loss
TP_PCT        = 0.014     # 1.4% take profit  (1:2.3 R:R)
MAX_POSITIONS = 3
SCAN_INTERVAL = 60        # 1 minute — faster signal capture
TIMEOUT_BARS  = 48        # close after 48 scans (~96 min)
TRAIL_TRIGGER = 0.008     # start trailing after 0.8% gain
TRAIL_DIST    = 0.005     # trail by 0.5%
PYRAMID_SCORE = 85        # pyramid if score >= 85
PYRAMID_EXTRA = 0.5       # add 50% more size on pyramid

ASSETS = {
    "ETHUSD":  "ethereum",
    "BTCUSD":  "bitcoin",
    "ADAUSD":  "cardano",
    "XRPUSD":  "ripple",
    "AVAXUSD": "avalanche-2",
    "UNIUSD":  "uniswap",
}
# Blacklisted per standing rules
BLACKLIST = {"SOLUSD", "LINKUSD"}
# Priority assets — proven 100% WR — get +8 score boost
PRIORITY_BOOST = {"ADAUSD": 8, "UNIUSD": 8, "EURUSD": 5, "GBPUSD": 5}
# XRP on watch — 50% WR, one more losing run = blacklist
XRP_TRADES = []

def log(msg):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    print(f"[{ts}] {msg}", flush=True)

# ── PRICE FEED ──────────────────────────────────────────────────────────────
def get_prices():
    ids = ",".join(ASSETS.values())
    url = f"https://api.coingecko.com/api/v3/simple/price?ids={ids}&vs_currencies=usd"
    req = urllib.request.Request(url, headers={"User-Agent": "NovaPaperTrader/2.0"})
    with urllib.request.urlopen(req, timeout=12) as r:
        raw = json.loads(r.read())
    return {sym: raw.get(cg, {}).get("usd", 0.0) for sym, cg in ASSETS.items()}

# ── BRAIN SCORING ───────────────────────────────────────────────────────────
def brain_score(symbol, side):
    try:
        if not os.path.exists(HISTORY_FILE):
            return 55
        hist = json.load(open(HISTORY_FILE))
        if len(hist) < 5:
            return 55
        # filter: same symbol OR same side
        relevant = [h for h in hist if h.get("symbol") == symbol or h.get("side") == side]
        if not relevant:
            relevant = hist
        wins  = sum(1 for h in relevant if h.get("result") == "WIN")
        total = len(relevant)
        wr    = wins / total
        conf  = min(total / 100.0, 1.0)      # confidence scales with sample size
        return min(100, int(40 + wr * 50 + conf * 10))
    except:
        return 55

# ── PORTFOLIO STATE ─────────────────────────────────────────────────────────
def load_state():
    if os.path.exists(STATE_FILE):
        try:
            return json.load(open(STATE_FILE))
        except:
            pass
    return {"cash": 10000.0, "positions": [], "wins": 0, "losses": 0, "total_pnl": 0.0}

def save_state(s):
    json.dump(s, open(STATE_FILE, "w"), indent=2)

# ── DB LOGGER ───────────────────────────────────────────────────────────────
def db_log(record):
    if not TOKEN:
        return
    payload = json.dumps(record)
    subprocess.run([
        "curl", "-s", "-X", "POST", BASE44_URL,
        "-H", "Content-Type: application/json",
        "-H", f"Authorization: Bearer {TOKEN}",
        "-H", "User-Agent: NovaPaperTrader/2.0",
        "-d", payload
    ], capture_output=True, timeout=10)

# ── BRAIN FEEDBACK ──────────────────────────────────────────────────────────
def teach_brain(symbol, side, result, pnl_pct):
    try:
        hist = []
        if os.path.exists(HISTORY_FILE):
            hist = json.load(open(HISTORY_FILE))
        hist.append({
            "symbol":    symbol,
            "side":      side,
            "features":  {"source": "paper_live"},
            "result":    result,
            "pnl_pct":   round(pnl_pct, 4),
            "logged_at": datetime.now(timezone.utc).isoformat()
        })
        hist = hist[-500:]
        json.dump(hist, open(HISTORY_FILE, "w"), indent=2)
    except:
        pass

# ── SINGLE SCAN ─────────────────────────────────────────────────────────────
def scan(state, prices, scan_num):
    positions = state["positions"]

    # 1. MANAGE EXITS
    still_open = []
    for pos in positions:
        sym    = pos["symbol"]
        side   = pos["side"]
        entry  = pos["entry"]
        sl     = pos["sl"]
        tp     = pos["tp"]
        opened = pos["opened_scan"]
        cur    = prices.get(sym, entry)

        if cur == 0:
            still_open.append(pos)
            continue

        hit_tp   = (side == "LONG" and cur >= tp) or (side == "SHORT" and cur <= tp)
        hit_sl   = (side == "LONG" and cur <= sl) or (side == "SHORT" and cur >= sl)
        timed_out = (scan_num - opened) >= TIMEOUT_BARS

        if hit_tp or hit_sl or timed_out:
            if hit_tp:
                exit_p  = tp;  pnl_pct = TP_PCT if side == "LONG" else -TP_PCT; result = "WIN";  reason = "TP"
            elif hit_sl:
                exit_p  = sl;  pnl_pct = -SL_PCT if side == "LONG" else SL_PCT; result = "LOSS"; reason = "SL"
            else:
                exit_p  = cur
                pnl_pct = (cur - entry) / entry if side == "LONG" else (entry - cur) / entry
                result  = "WIN" if pnl_pct > 0 else "LOSS"; reason = "TIMEOUT"

            pnl_usd = pnl_pct * SIZE
            state["cash"]      += SIZE + pnl_usd
            state["total_pnl"] += pnl_usd
            state["wins" if result == "WIN" else "losses"] += 1

            log(f"  ✅ CLOSE {side} {sym} → {result} {pnl_pct*100:+.2f}% ${pnl_usd:+.2f} [{reason}]")

            teach_brain(sym, side.lower(), result, pnl_pct * 100)
            db_log({
                "bot_name": "Paper Trader Live",
                "symbol":    sym,
                "signal":    side,
                "entry_price": round(entry, 6),
                "exit_price":  round(exit_p, 6),
                "pnl_usd":     round(pnl_usd, 2),
                "pnl_pct":     round(pnl_pct * 100, 4),
                "trade_status": result,
                "reason":    f"PAPER_{reason}",
                "scan_time": datetime.now(timezone.utc).isoformat()
            })
        else:
            unrealized = ((cur - entry) / entry if side == "LONG" else (entry - cur) / entry) * 100
            # ── TRAILING STOP ──────────────────────────────────────────
            gain = (cur - entry) / entry if side == "LONG" else (entry - cur) / entry
            if gain >= TRAIL_TRIGGER:
                if side == "LONG":
                    new_trail = cur - (cur * TRAIL_DIST)
                    if new_trail > pos.get("trail_stop", 0):
                        pos["trail_stop"] = new_trail
                        log(f"  📈 TRAIL {sym} stop → {new_trail:.4f}")
                else:
                    new_trail = cur + (cur * TRAIL_DIST)
                    if new_trail < pos.get("trail_stop", cur * 2):
                        pos["trail_stop"] = new_trail
                        log(f"  📉 TRAIL {sym} stop → {new_trail:.4f}")
            # Check if trailing stop hit
            trail_hit = False
            if "trail_stop" in pos:
                if side == "LONG" and cur <= pos["trail_stop"]:
                    trail_hit = True
                elif side == "SHORT" and cur >= pos["trail_stop"]:
                    trail_hit = True
            if trail_hit:
                pnl_pct2 = gain
                pnl_usd2 = pnl_pct2 * pos.get("size", SIZE)
                state["cash"] += pos.get("size", SIZE) + pnl_usd2
                state["total_pnl"] += pnl_usd2
                result2 = "WIN" if pnl_usd2 > 0 else "LOSS"
                state["wins" if result2 == "WIN" else "losses"] += 1
                log(f"  🛑 TRAIL CLOSE {sym} → {result2} {pnl_pct2*100:+.2f}% ${pnl_usd2:+.2f}")
                teach_brain(sym, side.lower(), result2, pnl_pct2 * 100)
                db_log({"bot_name":"Paper Trader Live","symbol":sym,"signal":side,
                        "entry_price":round(entry,6),"exit_price":round(cur,6),
                        "pnl_usd":round(pnl_usd2,2),"pnl_pct":round(pnl_pct2*100,4),
                        "trade_status":result2,"reason":"TRAIL_STOP",
                        "scan_time":datetime.now(timezone.utc).isoformat()})
                state["positions"].remove(pos)
                continue
            still_open.append(pos)

    state["positions"] = still_open

    # 2. LOOK FOR ENTRIES
    open_syms = {p["symbol"] for p in state["positions"]}

    if len(state["positions"]) < MAX_POSITIONS and state["cash"] >= SIZE:
        candidates = []
        for sym, cur in prices.items():
            if cur == 0 or sym in open_syms or sym in BLACKLIST:
                continue
            for side in ["LONG", "SHORT"]:
                score = brain_score(sym, side.lower())
                # Apply priority boost for proven high-WR assets
        boosted_score = score + PRIORITY_BOOST.get(sym, 0)
        if boosted_score >= MIN_SCORE:
                    candidates.append((score, sym, side, cur))

        candidates.sort(reverse=True)

        for score, sym, side, price in candidates:
            if sym in open_syms: continue
            if len(state["positions"]) >= MAX_POSITIONS: break

            sl = price * (1 - SL_PCT) if side == "LONG" else price * (1 + SL_PCT)
            tp = price * (1 + TP_PCT) if side == "LONG" else price * (1 - TP_PCT)

            state["positions"].append({
                "symbol": sym, "side": side,
                "entry": price, "sl": sl, "tp": tp,
                "score": score, "opened_scan": scan_num
            })
            state["cash"] -= SIZE
            open_syms.add(sym)
            log(f"  📈 OPEN {side} {sym} @ ${price:.4f} | Score:{score} | SL:${sl:.4f} TP:${tp:.4f}")

    return state

# ── MAIN LOOP ────────────────────────────────────────────────────────────────
def run_paper_trader():
    log("=== Nova Paper Trader LIVE — Real prices, Real learning ===")
    log(f"Capital: $10,000 | Size: ${SIZE}/trade | SL:{SL_PCT*100}% TP:{TP_PCT*100}% | Min Score:{MIN_SCORE}")

    state    = load_state()
    scan_num = 0

    while True:
        scan_num += 1
        try:
            prices = get_prices()
            state  = scan(state, prices, scan_num)
            save_state(state)

            total  = state["wins"] + state["losses"]
            wr     = state["wins"] / total * 100 if total else 0
            equity = state["cash"] + len(state["positions"]) * SIZE

            log(f"Scan#{scan_num} | {len(state['positions'])} open | "
                f"Closed:{total} WR:{wr:.0f}% PnL:${state['total_pnl']:+.2f} Equity:${equity:.2f}")

        except Exception as e:
            log(f"[ERROR] {e}")

        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    run_paper_trader()
