#!/usr/bin/env python3
"""
Nova Trading Bot Suite — Railway Deploy
Runs all bots in parallel threads, 24/7
TURBO LEARNING MODE: 2-min scan intervals, 50% confidence threshold
Auto-tightens to 65% confidence after 50 trades logged
"""
import threading, time, os, sys, json
import nova_candle_predictor as predictor
from datetime import datetime, timezone, timedelta

HISTORY_FILE       = "/tmp/candle_predictor_history.json"
TURBO_THRESHOLD    = 50    # trades before tightening confidence
TURBO_CONFIDENCE   = 50    # confidence during learning phase
NORMAL_CONFIDENCE  = 65    # confidence after 50 trades
SCAN_INTERVAL      = 120   # 2 minutes (was 300)
PATTERN_INTERVAL   = 180   # 3 minutes for pattern scanner (was 600)

def log(msg):
    print(f"[{datetime.now().strftime('%m/%d %H:%M')}] {msg}", flush=True)

def get_trade_count():
    # Try DB first (persistent across restarts)
    try:
        from nova_db_logger import get_trade_count as _db_count
        return _db_count()
    except: pass
    # Fallback to local file
    try:
        h = json.load(open(HISTORY_FILE))
        return len(h)
    except:
        return 0

def get_confidence_threshold():
    count = get_trade_count()
    if count >= TURBO_THRESHOLD:
        return NORMAL_CONFIDENCE
    return TURBO_CONFIDENCE

def is_ny_session():
    ET = timezone(timedelta(hours=-4))
    now = datetime.now(ET)
    return now.weekday() < 5 and (
        (now.hour == 9 and now.minute >= 30) or
        (10 <= now.hour <= 15) or
        (now.hour == 16 and now.minute == 0)
    )

def run_chop_scalper():
    import nova_chop_scalper as cs
    log("Chop Scalper v5 thread started [TURBO]")
    while True:
        try:
            cs.run()
        except Exception as e:
            log(f"[ChopScalper ERROR] {e}")
        time.sleep(SCAN_INTERVAL)

def run_breakout_trader():
    import nova_breakout_trader as bt
    log("Breakout Trader v4 thread started [TURBO]")
    while True:
        try:
            bt.run()
        except Exception as e:
            log(f"[BreakoutTrader ERROR] {e}")
        time.sleep(SCAN_INTERVAL)

def run_trend_rider():
    import nova_trend_rider as tr
    log("Trend Rider thread started [TURBO]")
    while True:
        try:
            tr.run()
        except Exception as e:
            log(f"[TrendRider ERROR] {e}")
        time.sleep(SCAN_INTERVAL)

def run_universal_scalper():
    import nova_universal_scalper as us
    log("Universal Scalper thread started [TURBO]")
    while True:
        try:
            us.run()
        except Exception as e:
            log(f"[UniversalScalper ERROR] {e}")
        time.sleep(SCAN_INTERVAL)

def run_forex_scalper():
    import nova_forex_scalper as fs
    log("Forex Scalper thread started [TURBO]")
    while True:
        try:
            fs.run()
        except Exception as e:
            log(f"[ForexScalper ERROR] {e}")
        time.sleep(SCAN_INTERVAL)

def run_futures_scalper():
    import nova_futures_scalper as futs
    log("Futures Scalper thread started [TURBO]")
    while True:
        try:
            futs.run()
        except Exception as e:
            log(f"[FuturesScalper ERROR] {e}")
        time.sleep(SCAN_INTERVAL)

def run_pattern_scanner():
    import nova_pattern_scanner as ps
    log("Pattern Scanner thread started [TURBO]")
    while True:
        try:
            ps.run()
        except Exception as e:
            log(f"[PatternScanner ERROR] {e}")
        time.sleep(PATTERN_INTERVAL)


def run_breakout_hunter():
    import nova_breakout_hunter_oanda as bh
    log("Breakout Hunter — OANDA Edition started [FTMO MODE]")
    while True:
        try:
            bh.run_once()
        except Exception as e:
            log(f"[BreakoutHunter ERROR] {e}")
        time.sleep(120)


def run_options_scanner():
    import nova_options_scanner as opts
    log("Options Scanner started [AAPL/NVDA/SPY/QQQ]")
    while True:
        try:
            opts.scan_once()
        except Exception as e:
            log(f"[OptionsScanner ERROR] {e}")
        time.sleep(300)

def run_forward_test_loop():
    import nova_paper_trader as pt
    log("Paper Trader LIVE started — real prices, $1k/trade, brain-scored entries")
    try:
        pt.run_paper_trader()
    except Exception as e:
        log(f"[PaperTrader ERROR] {e}")
        time.sleep(60)

def health_server():
    from http.server import HTTPServer, BaseHTTPRequestHandler
    import json

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == '/status':
                count      = get_trade_count()
                confidence = get_confidence_threshold()
                mode       = "TURBO LEARNING" if count < TURBO_THRESHOLD else "PRECISION"
                # get brain size
                brain_size = 0
                try:
                    import os as _os
                    hf = "/tmp/candle_predictor_history.json"
                    if _os.path.exists(hf):
                        brain_size = len(json.load(open(hf)))
                except: pass
                status = {
                    "status": "online",
                    "mode": mode,
                    "bots_running": 10,
                    "scan_interval_sec": SCAN_INTERVAL,
                    "confidence_threshold": confidence,
                    "trades_logged": count,
                    "trades_until_precision": max(0, TURBO_THRESHOLD - count),
                    "brain_training_records": brain_size,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "bots": [
                        "Chop Scalper v5",
                        "Breakout Trader v4",
                        "Trend Rider",
                        "Universal Scalper",
                        "Forex Scalper",
                        "Futures Scalper",
                        "Pattern Scanner",
                        "Breakout Hunter (OANDA)",
                        "Options Scanner",
                        "Paper Trader Live"
                    ]
                }
                body = json.dumps(status, indent=2).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()
        def log_message(self, *args): pass

    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(('0.0.0.0', port), Handler)
    log(f"Health server on port {port}")
    server.serve_forever()

if __name__ == "__main__":
    # ── SEED CANDLE PREDICTOR BRAIN ON STARTUP ──────────────────────────
    seed_file = os.path.join(os.path.dirname(__file__), "brain_seed.json")
    history_file = "/tmp/candle_predictor_history.json"
    try:
        if os.path.exists(seed_file):
            seed_data = json.load(open(seed_file))
            # Merge with any existing local history
            existing = []
            if os.path.exists(history_file):
                try: existing = json.load(open(history_file))
                except: pass
            # Only add seed records not already present (by logged_at)
            existing_times = {r.get("logged_at") for r in existing}
            new_records = [r for r in seed_data if r.get("logged_at") not in existing_times]
            merged = existing + new_records
            merged = merged[-500:]  # keep last 500
            json.dump(merged, open(history_file, "w"), indent=2)
            log(f"Brain seeded: {len(merged)} training records loaded ({len(new_records)} new)")
    except Exception as se:
        log(f"Brain seed warning: {se}")
    # ─────────────────────────────────────────────────────────────────────

    log("=== Nova Bot Suite — TURBO LEARNING MODE ===")
    log(f"Scan interval: {SCAN_INTERVAL}s | Confidence: {TURBO_CONFIDENCE}% → {NORMAL_CONFIDENCE}% after {TURBO_THRESHOLD} trades")
    log("Assets: ETH, ADA, XRP, UNI, EUR/USD, GBP/USD, BTC, AVAX")

    threads = [
        threading.Thread(target=health_server,         daemon=True, name="HealthServer"),
        threading.Thread(target=run_chop_scalper,      daemon=True, name="ChopScalper"),
        threading.Thread(target=run_breakout_trader,   daemon=True, name="BreakoutTrader"),
        threading.Thread(target=run_trend_rider,       daemon=True, name="TrendRider"),
        threading.Thread(target=run_universal_scalper, daemon=True, name="UniversalScalper"),
        threading.Thread(target=run_forex_scalper,     daemon=True, name="ForexScalper"),
        threading.Thread(target=run_futures_scalper,   daemon=True, name="FuturesScalper"),
        threading.Thread(target=run_pattern_scanner,   daemon=True, name="PatternScanner"),
        threading.Thread(target=run_breakout_hunter,    daemon=True, name="BreakoutHunter"),
        threading.Thread(target=run_options_scanner,     daemon=True, name="OptionsScanner"),
        threading.Thread(target=run_forward_test_loop,   daemon=True, name="PaperTrader"),
    ]

    for t in threads:
        t.start()
        log(f"Started: {t.name}")
        time.sleep(1)

    log("All 10 systems live — FTMO + Options + Brain validation running.")

    while True:
        alive  = [t.name for t in threads if t.is_alive()]
        count  = get_trade_count()
        conf   = get_confidence_threshold()
        mode   = "TURBO" if count < TURBO_THRESHOLD else "PRECISION"
        log(f"[{mode}] Threads: {len(alive)}/9 | Trades logged: {count} | Confidence: {conf}%")
        time.sleep(120)
