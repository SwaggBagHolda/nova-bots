#!/usr/bin/env python3
"""
Nova Learning Engine v2 — Learns after EVERY trade, not just nightly.
- Called by every bot immediately after a trade closes
- Uses expectancy formula: E = (WR * avg_win) - (LR * avg_loss)
- Only recommends param changes when: WR >= 60% AND PF >= 1.5 AND E > 0
- Writes /tmp/learned_params.json with approved changes
- Does NOT push changes — flags them for Nova approval gate
"""

import json, os, sys, math
from datetime import datetime, timezone
from collections import defaultdict

JOURNAL  = "/tmp/trade_journal.json"
LEARNED  = "/tmp/learned_params.json"
REPORT   = "/tmp/learning_report.json"
LOG      = "/tmp/learning_engine.log"

# ── Gate thresholds ────────────────────────────────────────────────────────
MIN_WR        = 60.0   # minimum win rate to recommend changes
MIN_PF        = 1.5    # minimum profit factor
MIN_TRADES    = 10     # minimum sample size before learning kicks in
MIN_EXPECTANCY= 0.0    # E = (WR*avg_win) - (LR*avg_loss) must be positive

def log(msg):
    ts = datetime.now().strftime("%m/%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    with open(LOG,"a") as f: f.write(line+"\n")

def load_journal():
    try:
        with open(JOURNAL) as f: return json.load(f)
    except: return []

def save_json(p, d):
    with open(p,"w") as f: json.dump(d,f,indent=2)

def expectancy(trades):
    """E = (WR * avg_win) - (LR * avg_loss) — true edge measurement"""
    if not trades: return 0
    wins   = [t for t in trades if t.get('win')]
    losses = [t for t in trades if not t.get('win')]
    wr  = len(wins)/len(trades)
    lr  = 1 - wr
    avg_w = sum(t['pnl_pct'] for t in wins)/len(wins) if wins else 0
    avg_l = sum(abs(t['pnl_pct']) for t in losses)/len(losses) if losses else 0
    return round((wr * avg_w) - (lr * avg_l), 5)

def analyze_trades(trades, label="all"):
    if not trades: return None
    wins   = [t for t in trades if t.get('win')]
    losses = [t for t in trades if not t.get('win')]
    total  = len(trades)
    wr     = len(wins)/total*100
    avg_w  = sum(t['pnl_pct'] for t in wins)/len(wins)     if wins   else 0
    avg_l  = sum(abs(t['pnl_pct']) for t in losses)/len(losses) if losses else 0.001
    pf     = avg_w / avg_l
    exp    = expectancy(trades)

    # By asset
    by_asset = defaultdict(list)
    for t in trades: by_asset[t['symbol']].append(t)
    asset_stats = {}
    for sym, ts in by_asset.items():
        w = [x for x in ts if x['win']]
        e = expectancy(ts)
        asset_stats[sym] = {
            "n": len(ts), "wr": round(len(w)/len(ts)*100,1),
            "avg_pnl": round(sum(x['pnl_pct'] for x in ts)/len(ts),4),
            "expectancy": e,
            "verdict": "✅ KEEP" if e > 0 and len(w)/len(ts) >= 0.5 else "⚠️ REVIEW"
        }

    # By session
    by_session = defaultdict(list)
    for t in trades: by_session[t.get('session','?')].append(t)
    session_stats = {s: {"n":len(ts),"wr":round(len([x for x in ts if x['win']])/len(ts)*100,1),
                         "avg_pnl":round(sum(x['pnl_pct'] for x in ts)/len(ts),4),
                         "expectancy":expectancy(ts)} for s,ts in by_session.items()}

    # By mode
    by_mode = defaultdict(list)
    for t in trades: by_mode[t.get('mode','?')].append(t)
    mode_stats = {m: {"n":len(ts),"wr":round(len([x for x in ts if x['win']])/len(ts)*100,1),
                      "avg_pnl":round(sum(x['pnl_pct'] for x in ts)/len(ts),4),
                      "expectancy":expectancy(ts)} for m,ts in by_mode.items()}

    # By exit
    by_exit = defaultdict(list)
    for t in trades: by_exit[t.get('exit_reason','?')].append(t)
    exit_stats = {e: {"n":len(ts),"pct":round(len(ts)/total*100,1),
                      "avg_pnl":round(sum(x['pnl_pct'] for x in ts)/len(ts),4)} for e,ts in by_exit.items()}

    # ADX / RSI patterns
    win_adx  = [t['indicators'].get('adx',0) for t in wins   if t.get('indicators')]
    loss_adx = [t['indicators'].get('adx',0) for t in losses if t.get('indicators')]
    win_rsi  = [t['indicators'].get('rsi',50) for t in wins   if t.get('indicators')]
    loss_rsi = [t['indicators'].get('rsi',50) for t in losses if t.get('indicators')]

    # Bars held
    win_bars  = [t.get('bars_held',0) for t in wins]
    loss_bars = [t.get('bars_held',0) for t in losses]
    timeout_pct = exit_stats.get('TIMEOUT',{}).get('pct',0)

    # ── Concrete param recommendations (only if gate passes) ────────────
    gate_passed = wr >= MIN_WR and pf >= MIN_PF and exp > MIN_EXPECTANCY and total >= MIN_TRADES
    recommendations = []
    param_changes   = {}

    if gate_passed:
        # ADX threshold adjustment
        if win_adx and loss_adx:
            win_avg_adx  = sum(win_adx)/len(win_adx)
            loss_avg_adx = sum(loss_adx)/len(loss_adx)
            if win_avg_adx > loss_avg_adx + 5:
                new_adx = round(win_avg_adx * 0.85)
                recommendations.append(f"Raise MIN_ADX to {new_adx} (wins avg {win_avg_adx:.1f} vs losses {loss_avg_adx:.1f})")
                param_changes['MIN_ADX'] = new_adx

        # RSI threshold
        if win_rsi and loss_rsi:
            win_avg_rsi  = sum(win_rsi)/len(win_rsi)
            recommendations.append(f"Best RSI entry zone: {win_avg_rsi:.1f} (tune entry filter)")
            param_changes['BEST_RSI_ENTRY'] = round(win_avg_rsi,1)

        # Stop multiplier — if too many stops, widen
        stop_pct = exit_stats.get('STOP',{}).get('pct',0) + exit_stats.get('TRAIL_STOP',{}).get('pct',0)
        if stop_pct > 55:
            recommendations.append(f"Stop rate {stop_pct:.0f}% — consider widening STOP_MULT by 0.1")
            param_changes['STOP_MULT_ADJUST'] = +0.1

        # Timeout rate — if high, exits are too slow
        if timeout_pct > 30:
            recommendations.append(f"Timeout rate {timeout_pct:.0f}% — reduce TIMEOUT from current or tighten entry")
            param_changes['TIMEOUT_ACTION'] = 'reduce'

        # Asset blacklisting
        for sym, s in asset_stats.items():
            if s['n'] >= 5 and s['expectancy'] < -0.02:
                recommendations.append(f"BLACKLIST {sym} — negative expectancy ({s['expectancy']:.5f})")
                if 'BLACKLIST' not in param_changes: param_changes['BLACKLIST'] = []
                param_changes['BLACKLIST'].append(sym)
            elif s['n'] >= 5 and s['expectancy'] > 0.05:
                recommendations.append(f"PRIORITIZE {sym} — high expectancy ({s['expectancy']:.5f})")
                if 'PRIORITIZE' not in param_changes: param_changes['PRIORITIZE'] = []
                param_changes['PRIORITIZE'].append(sym)

        # Best session
        if session_stats:
            best_s = max(session_stats, key=lambda x: session_stats[x]['expectancy'])
            worst_s= min(session_stats, key=lambda x: session_stats[x]['expectancy'])
            if session_stats[worst_s]['expectancy'] < -0.01 and session_stats[worst_s]['n'] >= 5:
                recommendations.append(f"AVOID {worst_s} session — expectancy {session_stats[worst_s]['expectancy']:.5f}")
                param_changes['AVOID_SESSION'] = worst_s
    else:
        # Not gate-passing yet — tell us how close we are
        deficit = []
        if wr     < MIN_WR:   deficit.append(f"WR {wr:.1f}% (need {MIN_WR}%)")
        if pf     < MIN_PF:   deficit.append(f"PF {pf:.2f} (need {MIN_PF})")
        if exp    <= 0:       deficit.append(f"Expectancy {exp:.5f} (need > 0)")
        if total  < MIN_TRADES: deficit.append(f"Only {total} trades (need {MIN_TRADES})")
        recommendations.append(f"⏳ Gate not passed: {' | '.join(deficit)}")

    return {
        "label":          label,
        "total_trades":   total,
        "wins":           len(wins),
        "losses":         len(losses),
        "win_rate":       round(wr,1),
        "avg_pnl_pct":    round(sum(t['pnl_pct'] for t in trades)/total,4),
        "profit_factor":  round(pf,2),
        "expectancy":     exp,
        "gate_passed":    gate_passed,
        "by_asset":       asset_stats,
        "by_session":     session_stats,
        "by_mode":        mode_stats,
        "by_exit":        exit_stats,
        "adx_on_wins":    round(sum(win_adx)/len(win_adx),1) if win_adx else None,
        "adx_on_losses":  round(sum(loss_adx)/len(loss_adx),1) if loss_adx else None,
        "rsi_on_wins":    round(sum(win_rsi)/len(win_rsi),1) if win_rsi else None,
        "bars_wins":      round(sum(win_bars)/len(win_bars),1) if win_bars else None,
        "bars_losses":    round(sum(loss_bars)/len(loss_bars),1) if loss_bars else None,
        "timeout_pct":    timeout_pct,
        "recommendations": recommendations,
        "param_changes":   param_changes if gate_passed else {}
    }

def run(trigger="manual", bot_name=None):
    """
    trigger: 'trade_close' | 'manual' | 'nightly'
    bot_name: if set, also runs per-bot analysis
    """
    journal = load_journal()
    if not journal:
        log("No trades in journal yet")
        return {"status": "empty"}

    log(f"Learning triggered by: {trigger} | {len(journal)} total trades")

    # Overall analysis
    overall = analyze_trades(journal, "OVERALL")

    # Per-bot analysis
    bots = list(set(t.get('bot_name','?') for t in journal))
    by_bot = {}
    for bot in bots:
        bt = [t for t in journal if t.get('bot_name') == bot]
        if len(bt) >= 3:
            by_bot[bot] = analyze_trades(bt, bot)

    # Save report
    report = {
        "generated":    datetime.now(timezone.utc).isoformat(),
        "trigger":      trigger,
        "overall":      overall,
        "by_bot":       by_bot,
        "gate_passed":  overall.get('gate_passed', False) if overall else False
    }
    save_json(REPORT, report)

    # Save learned params only if gate passed
    if overall and overall.get('gate_passed') and overall.get('param_changes'):
        learned = {
            "generated":    datetime.now(timezone.utc).isoformat(),
            "gate_passed":  True,
            "param_changes": overall['param_changes'],
            "based_on_trades": overall['total_trades'],
            "win_rate":     overall['win_rate'],
            "profit_factor":overall['profit_factor'],
            "expectancy":   overall['expectancy']
        }
        save_json(LEARNED, learned)
        log(f"✅ Gate passed! WR={overall['win_rate']}% PF={overall['profit_factor']} E={overall['expectancy']} → params saved")
    else:
        if overall:
            log(f"Gate not yet passed: WR={overall.get('win_rate')}% PF={overall.get('profit_factor')} E={overall.get('expectancy')} trades={overall.get('total_trades')}")

    # Print clean summary
    if overall:
        print(f"\n{'='*50}")
        print(f"LEARNING REPORT — {trigger.upper()}")
        print(f"{'='*50}")
        print(f"Trades: {overall['total_trades']} | WR: {overall['win_rate']}% | PF: {overall['profit_factor']} | E: {overall['expectancy']:+.5f}")
        print(f"Gate: {'✅ PASSED' if overall['gate_passed'] else '❌ NOT YET'}")
        print(f"\nAssets:")
        for sym, s in sorted(overall['by_asset'].items(), key=lambda x: x[1]['expectancy'], reverse=True):
            print(f"  {sym:12s} WR={s['wr']}% E={s['expectancy']:+.5f} n={s['n']} {s['verdict']}")
        print(f"\nSessions:")
        for sess, s in sorted(overall['by_session'].items(), key=lambda x: x[1]['expectancy'], reverse=True):
            print(f"  {sess:12s} WR={s['wr']}% E={s['expectancy']:+.5f} n={s['n']}")
        print(f"\nRecommendations:")
        for r in overall['recommendations']:
            print(f"  → {r}")

    return report

if __name__ == "__main__":
    trigger = sys.argv[1] if len(sys.argv) > 1 else "manual"
    run(trigger=trigger)
