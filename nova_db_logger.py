"""
Nova DB Logger — Posts trades to Base44 ScalperTrade entity
Replaces local file logging so data persists across Railway restarts.
"""
import urllib.request, json, os
from datetime import datetime, timezone

BASE44_URL = "https://api.base44.com/api/apps/69bf82ce8c526c379bdab3ce/entities/ScalperTrade"
TOKEN = os.environ.get("BASE44_SERVICE_TOKEN", "")

def post_trade(bot_name, symbol, signal, entry_price, exit_price, pnl_usd, pnl_pct, reason, trade_status="CLOSED"):
    """Post a completed trade to Base44 DB for persistent learning."""
    try:
        record = {
            "bot_name": bot_name,
            "symbol": symbol,
            "signal": signal,
            "entry_price": round(float(entry_price), 6),
            "exit_price": round(float(exit_price), 6),
            "pnl_usd": round(float(pnl_usd), 2),
            "pnl_pct": round(float(pnl_pct), 4),
            "trade_status": trade_status,
            "reason": reason,
            "scan_time": datetime.now(timezone.utc).isoformat()
        }
        data = json.dumps(record).encode()
        req = urllib.request.Request(
            BASE44_URL,
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {TOKEN}"
            }
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            result = json.loads(resp.read())
            print(f"  [DB] Trade logged: {bot_name} {symbol} {pnl_pct:+.3f}% | id={result.get('id','?')}")
            return result
    except Exception as e:
        print(f"  [DB ERROR] Could not log trade: {e}")
        return None

def get_trade_count():
    """Get total trades logged in DB for confidence threshold management."""
    try:
        req = urllib.request.Request(
            BASE44_URL + "?limit=1",
            method="GET",
            headers={"Authorization": f"Bearer {TOKEN}"}
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            return data.get("count", 0)
    except Exception as e:
        print(f"  [DB ERROR] Could not get trade count: {e}")
        return 0
