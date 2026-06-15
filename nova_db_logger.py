"""
Nova DB Logger — Posts trades to Base44 ScalperTrade entity
Persists across Railway restarts so the learning engine always has data.
"""
import urllib.request, json, os
from datetime import datetime, timezone

BASE44_URL = "https://app.base44.com/api/apps/69bf82ce8c526c379bdab3ce/entities/ScalperTrade"
COUNT_URL  = "https://app.base44.com/api/apps/69bf82ce8c526c379bdab3ce/entities/ScalperTrade"
TOKEN = os.environ.get("BASE44_SERVICE_TOKEN", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiI1NTBiODExZS1kMGNlLTRkYjAtODVmZS0wYTE4MzZiZTVmNDciLCJjbGllbnRfaWQiOiI1NTBiODExZS1kMGNlLTRkYjAtODVmZS0wYTE4MzZiZTVmNDciLCJhcHBfaWQiOiI2OWJmODJjZThjNTI2YzM3OWJkYWIzY2UiLCJhdWQiOiJiYXNlNDRfYXBpIiwic2NvcGUiOiJhcHAuYWNjZXNzIiwiZXhwIjoxNzgxNTQ4MDI4LCJpYXQiOjE3ODE1NDQ0Mjh9.-j7b7k8WevY27CMESHTC-qpSuRjJfQGc4exMg9sL5aE")  # fallback for Railway

def post_trade(bot_name, symbol, signal, entry_price, exit_price, pnl_usd, pnl_pct, reason, trade_status="CLOSED"):
    """Post a completed trade to Base44 DB for persistent learning."""
    if not TOKEN:
        print(f"  [DB] No token — skipping DB log for {symbol}")
        return None
    try:
        record = {
            "bot_name":    bot_name,
            "symbol":      symbol,
            "signal":      signal,
            "entry_price": round(float(entry_price), 6),
            "exit_price":  round(float(exit_price), 6),
            "pnl_usd":     round(float(pnl_usd), 2),
            "pnl_pct":     round(float(pnl_pct), 4),
            "trade_status":trade_status,
            "reason":      reason,
            "scan_time":   datetime.now(timezone.utc).isoformat()
        }
        data = json.dumps(record).encode()
        req = urllib.request.Request(
            BASE44_URL,
            data=data,
            method="POST",
            headers={
                "Content-Type":  "application/json",
                "Authorization": f"Bearer {TOKEN}"
            }
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            result = json.loads(resp.read())
            status = "WIN" if pnl_usd > 0 else "LOSS"
            print(f"  [DB ✅] {bot_name} {symbol} {pnl_pct:+.3f}% ${pnl_usd:+.1f} [{status}] id={result.get('id','?')}")
            return result
    except Exception as e:
        print(f"  [DB ❌] Could not log trade: {e}")
        return None

def get_trade_count():
    """Get total trades logged in DB — used for confidence threshold management."""
    if not TOKEN:
        return 0
    try:
        req = urllib.request.Request(
            COUNT_URL,
            method="GET",
            headers={"Authorization": f"Bearer {TOKEN}"}
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())
            return data.get("count", 0)
    except Exception as e:
        print(f"  [DB ❌] Could not get trade count: {e}")
        return 0
