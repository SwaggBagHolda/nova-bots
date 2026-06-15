"""
Nova Shared Logger — writes to Base44 DB + in-memory buffer
All bots import this. Works with or without DB token.
"""
import os, requests, threading
from datetime import datetime, timezone

B44_TOKEN = os.environ.get("BASE44_SERVICE_TOKEN", "")
B44_URL   = "https://app.base44.com/api/apps/69bf82ce8c526c379bdab3ce/entities/ScalperTrade"

_lock       = threading.Lock()
_live_trades = []   # shared ring buffer — last 500 trades

def log_trade(bot_name, symbol, signal, entry, exit_price, pnl_usd, pnl_pct, status, reason):
    """Log a completed trade to memory + DB (if token available)"""
    record = {
        "bot":     bot_name,
        "symbol":  symbol,
        "signal":  signal,
        "entry":   round(float(entry), 5),
        "exit":    round(float(exit_price), 5),
        "pnl_usd": round(float(pnl_usd), 2),
        "pnl_pct": round(float(pnl_pct), 4),
        "status":  status,
        "reason":  reason,
        "time":    datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    }
    with _lock:
        _live_trades.append(record)
        if len(_live_trades) > 500:
            _live_trades.pop(0)

    # DB write — non-blocking, best-effort
    if B44_TOKEN:
        try:
            requests.post(B44_URL, json={
                "bot_name":    bot_name,
                "symbol":      symbol,
                "scan_time":   record["time"],
                "signal":      signal,
                "entry_price": record["entry"],
                "exit_price":  record["exit"],
                "pnl_usd":     record["pnl_usd"],
                "pnl_pct":     record["pnl_pct"],
                "trade_status": status,
                "reason":       reason
            }, headers={"Authorization": f"Bearer {B44_TOKEN}", "Content-Type": "application/json"},
            timeout=6)
        except:
            pass  # DB down? no problem — still in memory

def get_stats():
    with _lock:
        trades = list(_live_trades)
    if not trades:
        return {"total": 0, "wins": 0, "losses": 0, "win_rate": 0, "total_pnl": 0, "recent": []}
    wins   = [t for t in trades if t["status"] == "WIN"]
    losses = [t for t in trades if t["status"] == "LOSS"]
    total_pnl = sum(t["pnl_usd"] for t in trades)
    return {
        "total":     len(trades),
        "wins":      len(wins),
        "losses":    len(losses),
        "win_rate":  round(len(wins)/len(trades)*100, 1),
        "total_pnl": round(total_pnl, 2),
        "avg_pnl":   round(total_pnl/len(trades), 2),
        "recent":    trades[-20:][::-1]
    }
