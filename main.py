#!/usr/bin/env python3
"""
Nova Trading Bot Suite — Railway Deploy
Runs all bots in parallel threads, 24/7
"""
import threading, time, os, sys
import nova_candle_predictor as predictor
from datetime import datetime, timezone, timedelta

TURBO_CONFIDENCE = 50
NORMAL_CONFIDENCE = 65
TURBO_THRESHOLD = 50

def log(msg):
    print(f"[{datetime.now().strftime('%m/%d %H:%M')}] {msg}", flush=True)

def is_ny_session():
    ET = timezone(timedelta(hours=-4))
    now = datetime.now(ET)
    # Mon-Fri, 9:30 AM - 4:00 PM ET
    return now.weekday() < 5 and (
        (now.hour == 9 and now.minute >= 30) or
        (10 <= now.hour <= 15) or
        (now.hour == 16 and now.minute == 0)
    )

def run_chop_scalper():
    """Chop Scalper v5 — BB+RSI mean reversion, NY session + 24/7 crypto"""
    import nova_chop_scalper as cs
    log("Chop Scalper v5 thread started")
    while True:
        try:
            cs.run()
        except Exception as e:
            log(f"[ChopScalper ERROR] {e}")
        time.sleep(120)  # 5 min interval

def run_breakout_trader():
    """Breakout Trader v4 — ADX breakout with trailing stops"""
    import nova_breakout_trader as bt
    log("Breakout Trader v4 thread started")
    while True:
        try:
            bt.run()
        except Exception as e:
            log(f"[BreakoutTrader ERROR] {e}")
        time.sleep(120)

def run_trend_rider():
    """Trend Rider — pure trend following, no fixed target"""
    import nova_trend_rider as tr
    log("Trend Rider thread started")
    while True:
        try:
            tr.run()
        except Exception as e:
            log(f"[TrendRider ERROR] {e}")
        time.sleep(120)

def run_universal_scalper():
    """Universal Scalper — multi-strategy, multi-asset"""
    import nova_universal_scalper as us
    log("Universal Scalper thread started")
    while True:
        try:
            us.run()
        except Exception as e:
            log(f"[UniversalScalper ERROR] {e}")
        time.sleep(120)

def run_forex_scalper():
    """Forex Scalper — EUR/USD, GBP/USD during market hours"""
    import nova_forex_scalper as fs
    log("Forex Scalper thread started")
    while True:
        try:
            fs.run()
        except Exception as e:
            log(f"[ForexScalper ERROR] {e}")
        time.sleep(120)

def run_futures_scalper():
    """Futures Scalper — ES/NQ proxy via ETFs"""
    import nova_futures_scalper as futs
    log("Futures Scalper thread started")
    while True:
        try:
            futs.run()
        except Exception as e:
            log(f"[FuturesScalper ERROR] {e}")
        time.sleep(120)

def run_pattern_scanner():
    """Pattern Scanner — whale watcher, fires alert on signal >= 7/10"""
    import nova_pattern_scanner as ps
    log("Pattern Scanner thread started")
    while True:
        try:
            ps.run()
        except Exception as e:
            log(f"[PatternScanner ERROR] {e}")
        time.sleep(600)  # 10 min

def health_server():
    """Simple HTTP status endpoint on port 8080"""
    from http.server import HTTPServer, BaseHTTPRequestHandler
    import json

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == '/status':
                status = {
                    "status": "online",
                    "bots_running": 7,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "bots": [
                        "Chop Scalper v5",
                        "Breakout Trader v4",
                        "Trend Rider",
                        "Universal Scalper",
                        "Forex Scalper",
                        "Futures Scalper",
                        "Pattern Scanner"
                    ]
                }
                body = json.dumps(status).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()
        def log_message(self, *args): pass  # silence request logs

    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(('0.0.0.0', port), Handler)
    log(f"Health server running on port {port}")
    server.serve_forever()

if __name__ == "__main__":
    log("=== Nova Bot Suite Starting on Railway ===")
    log("Assets: ETH, ADA, XRP, UNI, EUR/USD, GBP/USD, BTC, AVAX")

    threads = [
        threading.Thread(target=health_server,       daemon=True, name="HealthServer"),
        threading.Thread(target=run_chop_scalper,    daemon=True, name="ChopScalper"),
        threading.Thread(target=run_breakout_trader, daemon=True, name="BreakoutTrader"),
        threading.Thread(target=run_trend_rider,     daemon=True, name="TrendRider"),
        threading.Thread(target=run_universal_scalper, daemon=True, name="UniversalScalper"),
        threading.Thread(target=run_forex_scalper,   daemon=True, name="ForexScalper"),
        threading.Thread(target=run_futures_scalper, daemon=True, name="FuturesScalper"),
        threading.Thread(target=run_pattern_scanner, daemon=True, name="PatternScanner"),
    ]

    for t in threads:
        t.start()
        log(f"Started: {t.name}")
        time.sleep(2)

    log("All bots live. Running 24/7.")

    # Keep main thread alive
    while True:
        alive = [t.name for t in threads if t.is_alive()]
        log(f"Active threads: {len(alive)}/8 — {', '.join(alive)}")
        time.sleep(120)
