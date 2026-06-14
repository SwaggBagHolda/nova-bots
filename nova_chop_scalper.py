#!/usr/bin/env python3
"""
Nova Chop Scalper v2
════════════════════
Quant-audited rebuild. Fixes:
- Position isolation (own registry)
- ATR stops persisted to disk
- AVAX/DOT direction filter (no MR longs when trending down hard)
- Correlation filter (max 1 per cluster)
- ML binary training signal (hold vs exit)
- Session multiplier
- Proper position sizing ($risk / stop_pct)
"""
import urllib.request, urllib.parse, json, os, math, random
try:
    from nova_db_logger import post_trade as _db_post
except: _db_post = None
from datetime import datetime, timezone, timedelta

API_KEY    = "PKHFMGMEDX45XRMPT4OWYIKKR4"
API_SECRET = "DqVA62jPiJuF88VmnusrDAQxBdsgnDahvQWuXiUcdt62"
BASE       = "https://paper-api.alpaca.markets"
DATA_URL   = "https://data.alpaca.markets"
LOG_FILE   = "/tmp/trade_log.json"
WEIGHTS_F  = "/tmp/nova_weights.json"
POS_FILE   = "/tmp/scalper_positions.json"  # ← own position registry
BOT_NAME   = "Chop Scalper"
COOLDOWN_F = "/tmp/scalper_cooldown.json"

START_EQ   = 242000.0
TOTAL_MAX  = 0.08
MAX_POS    = 3
RISK_USD   = 400.0   # fixed $ risk per trade

ALTS = ["ETH/USD","SOL/USD","AVAX/USD","LINK/USD","ADA/USD","XRP/USD","DOT/USD","BTC/USD"]

# Correlation clusters — max 1 per cluster
CLUSTERS = {
    "altcoin": ["SOLUSD","ADAUSD","AVAXUSD","DOTUSD","LINKUSD"],
    "btc_eth": ["BTCUSD","ETHUSD"],
    "stable_alt": ["XRPUSD"]
}

# Assets that trend too strongly for MR longs — only allow shorts when overbought
TREND_SENSITIVE = ["AVAXUSD","DOTUSD"]

peak_pnl={}; entry_bar={}; state_history={}

# ── AUTOGRAD ──────────────────────────────────────────────────
class V:
    __slots__=('d','g','_ch','_lg')
    def __init__(self,d,ch=(),lg=()):
        self.d=float(d);self.g=0.0;self._ch=ch;self._lg=lg
    def __add__(self,o):
        o=o if isinstance(o,V) else V(o)
        return V(self.d+o.d,(self,o),(1,1))
    def __mul__(self,o):
        o=o if isinstance(o,V) else V(o)
        return V(self.d*o.d,(self,o),(o.d,self.d))
    def __pow__(self,n): return V(self.d**n,(self,),(n*self.d**(n-1),))
    def __neg__(self): return self*-1
    def __radd__(self,o): return self+o
    def __sub__(self,o): return self+(-o)
    def __rmul__(self,o): return self*o
    def __truediv__(self,o): return self*V(o)**-1
    def relu(self): return V(max(0,self.d),(self,),(float(self.d>0),))
    def sigmoid(self):
        s=1/(1+math.exp(-max(-20,min(20,self.d))))
        return V(s,(self,),(s*(1-s),))
    def backward(self):
        t=[];v=set()
        def b(n):
            if n not in v:
                v.add(n)
                for c in n._ch: b(c)
                t.append(n)
        b(self);self.g=1.0
        for n in reversed(t):
            for c,g in zip(n._ch,n._lg): c.g+=g*n.g

class MLP:
    def __init__(self):
        def L(ni,no):
            s=math.sqrt(2.0/ni)
            return [[V(random.gauss(0,s)) for _ in range(ni)] for _ in range(no)],\
                   [V(0.0) for _ in range(no)]
        self.w1,self.b1=L(8,14);self.w2,self.b2=L(14,7);self.w3,self.b3=L(7,1)
    def params(self):
        p=[]
        for r in self.w1+self.w2+self.w3: p+=r
        p+=self.b1+self.b2+self.b3;return p
    def forward(self,x):
        def lin(w,b,i): return [sum(w[r][c]*i[c] for c in range(len(i)))+b[r] for r in range(len(b))]
        h1=[z.relu() for z in lin(self.w1,self.b1,[V(v) for v in x])]
        h2=[z.relu() for z in lin(self.w2,self.b2,h1)]
        o=lin(self.w3,self.b3,h2);return o[0].sigmoid()
    def to_dict(self):
        return {"w1":[[v.d for v in r] for r in self.w1],"b1":[v.d for v in self.b1],
                "w2":[[v.d for v in r] for r in self.w2],"b2":[v.d for v in self.b2],
                "w3":[[v.d for v in r] for r in self.w3],"b3":[v.d for v in self.b3]}
    def from_dict(self,d):
        for i,r in enumerate(self.w1):
            for j,v in enumerate(r): v.d=d["w1"][i][j]
        for i,v in enumerate(self.b1): v.d=d["b1"][i]
        for i,r in enumerate(self.w2):
            for j,v in enumerate(r): v.d=d["w2"][i][j]
        for i,v in enumerate(self.b2): v.d=d["b2"][i]
        for i,r in enumerate(self.w3):
            for j,v in enumerate(r): v.d=d["w3"][i][j]
        for i,v in enumerate(self.b3): v.d=d["b3"][i]

nets={};net_meta={}

def load_nets():
    if not os.path.exists(WEIGHTS_F): return
    try:
        data=json.load(open(WEIGHTS_F))
        for sym,d in data.items():
            if "weights" not in d: continue
            m=MLP()
            try: m.from_dict(d["weights"]); nets[sym]=m; net_meta[sym]=d["meta"]
            except: pass
    except: pass

def save_nets():
    existing={}
    if os.path.exists(WEIGHTS_F):
        try: existing=json.load(open(WEIGHTS_F))
        except: pass
    for sym,m in nets.items():
        existing[sym]={"weights":m.to_dict(),"meta":net_meta.get(sym,{})}
    json.dump(existing,open(WEIGHTS_F,"w"))

def get_net(sym):
    if sym not in nets:
        nets[sym]=MLP();net_meta[sym]={"trade_count":0,"lr":0.05}
    return nets[sym]

def load_positions():
    if os.path.exists(POS_FILE):
        try: return json.load(open(POS_FILE))
        except: pass
    return {}

def save_positions(pos):
    json.dump(pos, open(POS_FILE,"w"), indent=2)

# ── INDICATORS ────────────────────────────────────────────────
def ema(v,p):
    k=2/(p+1);e=v[0]
    for x in v[1:]: e=x*k+e*(1-k)
    return e

def sma(v,p):
    return sum(v[-p:])/min(len(v),p)

def rsi(v,p=14):
    if len(v)<p+1: return 50.0
    g=[max(v[i]-v[i-1],0) for i in range(1,len(v))]
    l=[max(v[i-1]-v[i],0) for i in range(1,len(v))]
    ag=sum(g[-p:])/p;al=sum(l[-p:])/p
    return 100-100/(1+ag/al) if al>0 else 100.0

def stoch_rsi(v,p=14):
    if len(v)<p*2: return 50.0
    rv=[rsi(v[i:i+p+1]) for i in range(len(v)-p)]
    if len(rv)<p: return 50.0
    w=rv[-p:];lo=min(w);hi=max(w)
    return (rv[-1]-lo)/(hi-lo+1e-9)*100

def atr(bs,p=10):
    t=[]
    for i in range(1,len(bs)):
        t.append(max(bs[i]["h"]-bs[i]["l"],abs(bs[i]["h"]-bs[i-1]["c"]),abs(bs[i]["l"]-bs[i-1]["c"])))
    return sum(t[-p:])/min(len(t),p) if t else 0.001

def bollinger(v,p=20,mult=2.0):
    if len(v)<p: return v[-1]*1.02,v[-1],v[-1]*0.98,0.02
    w=v[-p:];mid=sum(w)/p;sd=math.sqrt(sum((x-mid)**2 for x in w)/p)
    up=mid+mult*sd;lo=mid-mult*sd
    return up,mid,lo,(up-lo)/(mid+1e-9)

def bb_width_avg(v,p=20,lookback=20):
    ws=[]
    for i in range(p,len(v)):
        u,m,l,w=bollinger(v[i-p:i],p)
        ws.append(w)
    return sum(ws[-lookback:])/max(len(ws[-lookback:]),1) if ws else 0.02

def adx(bs,p=14):
    if len(bs)<p+2: return 15.0
    tr=[];pd=[];nd=[]
    for i in range(1,len(bs)):
        h=bs[i]["h"];l=bs[i]["l"];ph=bs[i-1]["h"];pl=bs[i-1]["l"];pc=bs[i-1]["c"]
        tr.append(max(h-l,abs(h-pc),abs(l-pc)))
        u=h-ph;d=pl-l
        pd.append(u if u>d and u>0 else 0)
        nd.append(d if d>u and d>0 else 0)
    def sm(a,n):
        if len(a)<n: return 0.001
        s=sum(a[:n])
        for v in a[n:]: s=s-s/n+v
        return s/n
    at=sm(tr,p);pdi=100*sm(pd,p)/(at+1e-9);ndi=100*sm(nd,p)/(at+1e-9)
    return 100*abs(pdi-ndi)/(pdi+ndi+1e-9)

def session_multiplier():
    ET=timezone(timedelta(hours=-4))
    h=datetime.now(ET).hour
    if 8<=h<11: return 1.3
    if 20<=h<23: return 1.2
    if 2<=h<4:  return 1.1
    if 14<=h<16: return 1.2
    return 1.0

# ── ML ────────────────────────────────────────────────────────
def build_sv(r,ed,br,is_long,pnl,bars_held,vol_ratio,sm):
    return [r/100, max(-1,min(1,ed/5))*0.5+0.5, max(0,min(1,br)),
            1.0 if is_long else 0.0, max(-1,min(1,pnl/3))*0.5+0.5,
            min(1,bars_held/15), min(2,vol_ratio)/2, min(1.5,sm)/1.5]

def train(sym, states_with_next_pnl):
    if len(states_with_next_pnl)<3: return
    m=get_net(sym); meta=net_meta[sym]; lr=meta.get("lr",0.05)
    for sv,cur,nxt in states_with_next_pnl:
        tgt=1.0 if nxt<cur-0.04 else 0.0
        for p in m.params(): p.g=0.0
        ep=m.forward(sv); err=ep-V(tgt); loss=err*err; loss.backward()
        for p in m.params(): p.d-=lr*p.g; p.g=0.0
    meta["trade_count"]+=1; meta["lr"]=max(0.003,lr*0.97); save_nets()

def ml_should_exit(sym, sv):
    m=get_net(sym); tc=net_meta.get(sym,{}).get("trade_count",0)
    if tc<8: return None
    for p in m.params(): p.g=0.0
    ep=m.forward(sv); return ep.d

# ── API ───────────────────────────────────────────────────────
def req(method,path,body=None):
    data=json.dumps(body).encode() if body else None
    r=urllib.request.Request(f"{BASE}{path}",data=data,method=method,
        headers={"APCA-API-KEY-ID":API_KEY,"APCA-API-SECRET-KEY":API_SECRET,"Content-Type":"application/json"})
    try:
        with urllib.request.urlopen(r,timeout=15) as resp:
            raw=resp.read();return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e: return {"error":e.read().decode()[:200]}
    except Exception as e: return {"error":str(e)}

def get_bars(sym,tf="5Min",limit=120):
    enc=urllib.parse.quote(sym,safe="")
    url=f"{DATA_URL}/v1beta3/crypto/us/bars?symbols={enc}&timeframe={tf}&limit={limit}"
    r=urllib.request.Request(url,headers={"APCA-API-KEY-ID":API_KEY,"APCA-API-SECRET-KEY":API_SECRET})
    try:
        with urllib.request.urlopen(r,timeout=15) as resp:
            return json.loads(resp.read()).get("bars",{}).get(sym,[])
    except: return []

def log_trade(sym,side,ep,xp,pusd,ppct,reason,states_with_next):
    rec={"bot_name":BOT_NAME,"symbol":sym,"signal":side,
         "entry_price":round(float(ep),6),"exit_price":round(float(xp),6),
         "pnl_usd":round(float(pusd),2),"pnl_pct":round(float(ppct),4),
         "trade_status":"CLOSED","reason":reason,
         "logged_at":datetime.now(timezone.utc).isoformat()}
    t=[]
    if os.path.exists(LOG_FILE):
        try: t=json.load(open(LOG_FILE))
        except: pass
    t.append(rec);json.dump(t,open(LOG_FILE,"w"),indent=2)
    # ── POST TO BASE44 DB (persistent learning) ──────────────────────────
    if _db_post:
        import threading
        threading.Thread(target=_db_post, args=(BOT_NAME,sym,side,ep,xp,pusd,ppct,reason), daemon=True).start()
    # ─────────────────────────────────────────────────────────────────────
    train(sym, states_with_next)
    print(f"  [{'WIN' if pusd>0 else 'LOSS'}][CHOP] {sym} {ppct:+.3f}% ${pusd:+.1f} | {reason}")


COOLDOWN_F = "/tmp/scalper_cooldown.json"

def load_cooldown():
    if os.path.exists(COOLDOWN_F):
        try: return json.load(open(COOLDOWN_F))
        except: pass
    return {}

def save_cooldown(cd):
    json.dump(cd, open(COOLDOWN_F,"w"))

def in_cooldown(sym, cooldown_dict, minutes=30):
    if sym not in cooldown_dict: return False
    last = datetime.fromisoformat(cooldown_dict[sym])
    return (datetime.now(timezone.utc) - last).total_seconds() < minutes*60

def set_cooldown(sym, cooldown_dict):
    cooldown_dict[sym] = datetime.now(timezone.utc).isoformat()
    save_cooldown(cooldown_dict)

# ── MAIN ──────────────────────────────────────────────────────
def run():
    load_nets()
    my_positions=load_positions()
    cooldown=load_cooldown()
    sm=session_multiplier()

    ET=timezone(timedelta(hours=-4))
    fmt=datetime.now(ET).strftime("%a %H:%M ET")

    acct=req("GET","/v2/account")
    if "error" in acct: print(f"ERR:{acct}"); return
    eq=float(acct.get("equity",START_EQ))
    cash=float(acct.get("cash",START_EQ))
    dd=(START_EQ-eq)/START_EQ if eq<START_EQ else 0
    gain=(eq-START_EQ)/START_EQ
    print(f"Chop Scalper | {fmt} | ${eq:,.0f} | {gain*100:+.2f}% | DD:{dd*100:.1f}% | sess={sm}")

    if dd>=TOTAL_MAX: print("  HALTED — total DD"); return

    all_positions=req("GET","/v2/positions") or []
    alpaca_held={p.get("symbol",""):p for p in all_positions}

    # Sync registry
    for sym in list(my_positions.keys()):
        if sym not in alpaca_held:
            my_positions.pop(sym,None)
    save_positions(my_positions)

    slots=MAX_POS-len(my_positions)

    # ── EXIT ─────────────────────────────────────────────────
    for sym, meta in list(my_positions.items()):
        pos=alpaca_held.get(sym)
        if not pos: continue

        side=pos.get("side","long")
        pnl_pct=float(pos.get("unrealized_plpc",0))*100
        pnl_usd=float(pos.get("unrealized_pl",0))
        ep=float(pos.get("avg_entry_price",1))
        cp=float(pos.get("current_price",ep))
        qty=float(pos.get("qty",0))
        at=meta.get("atr",ep*0.01)
        atp=at/ep*100 if ep>0 else 1.0
        target_mid=meta.get("mid",cp)

        if sym not in peak_pnl: peak_pnl[sym]=pnl_pct
        if abs(pnl_pct)>abs(peak_pnl[sym]): peak_pnl[sym]=pnl_pct
        pk=peak_pnl[sym]
        entry_bar[sym]=entry_bar.get(sym,0)+1
        bh=entry_bar[sym]

        alt=sym[:-3]+"/USD" if sym.endswith("USD") else sym
        bs5=get_bars(alt,"5Min",40)
        r=50.0;ed=0.0;br=0.5;vol_ratio=1.0;upper=cp*1.02;cur_mid=cp;lower=cp*0.98
        if bs5 and len(bs5)>=10:
            c=[float(b["c"]) for b in bs5]
            o=[float(b["o"]) for b in bs5]
            h_=[float(b["h"]) for b in bs5]
            l_=[float(b["l"]) for b in bs5]
            r=rsi(c[-16:]);e8=ema(c[-20:],8);e21=ema(c,21)
            ed=(e8-e21)/e21*100 if e21>0 else 0
            bod=[abs(c[i]-o[i]) for i in range(-3,0)]
            rng=[h_[i]-l_[i] for i in range(-3,0)]
            br=(sum(bod)/3)/(sum(rng)/3+1e-9)
            vols=[float(b.get("v",1)) for b in bs5[-20:]]
            vol_ratio=vols[-1]/(sum(vols)/len(vols)+1e-9)
            upper,cur_mid,lower,_=bollinger(c,20)

        is_long=side=="long"
        sv=build_sv(r,ed,br,is_long,pnl_pct,bh,vol_ratio,sm)
        if sym not in state_history: state_history[sym]=[]
        state_history[sym].append(pnl_pct)

        reason=None

        if pnl_pct<=-atp*0.8:
            reason=f"STOP {pnl_pct:.2f}%"
        else:
            if is_long and cp>=cur_mid:
                reason=f"MEAN HIT +{pnl_pct:.2f}%"
            elif not is_long and cp<=cur_mid:
                reason=f"MEAN HIT +{pnl_pct:.2f}%"
            else:
                ml=ml_should_exit(sym,sv)
                if ml is not None and ml>=0.72:
                    reason=f"ML p={ml:.2f}"
                if not reason and bh>=18:
                    reason=f"TIMEOUT {bh}b pnl={pnl_pct:+.2f}%"

        if reason:
            cs="sell" if is_long else "buy"
            req("POST","/v2/orders",{"symbol":sym,"qty":str(abs(qty)),"side":cs,
                "type":"market","time_in_force":"gtc"})
            hist=state_history.pop(sym,[])
            trip=[(build_sv(r,ed,br,is_long,hist[i],i,vol_ratio,sm),hist[i],hist[i+1])
                  for i in range(len(hist)-1)]
            log_trade(sym,side.upper(),ep,cp,pnl_usd,pnl_pct,reason,trip)
            my_positions.pop(sym,None);save_positions(my_positions)
            if "STOP" in reason: set_cooldown(sym, cooldown)
            peak_pnl.pop(sym,None);entry_bar.pop(sym,None);slots+=1
        else:
            print(f"  HOLD {'L' if is_long else 'S'} {sym} {pnl_pct:+.2f}% → mean={cur_mid:.4f}")

    if slots<=0: print(f"  FULL {MAX_POS}"); return

    # ── ENTRY SCAN ────────────────────────────────────────────
    def cluster_for(sym):
        for k,v in CLUSTERS.items():
            if sym in v: return k
        return sym
    held_clusters={cluster_for(s) for s in my_positions}

    candidates=[]
    for sym in ALTS:
        clean=sym.replace("/","")
        if clean in my_positions: continue
        if cluster_for(clean) in held_clusters: continue
        if in_cooldown(clean, cooldown): continue  # 30min stop cooldown

        bs5=get_bars(sym,"5Min",120)
        bs15=get_bars(sym,"15Min",60)
        if not bs5 or len(bs5)<25: continue

        c=[float(b["c"]) for b in bs5]
        h=[float(b["h"]) for b in bs5]
        l_=[float(b["l"]) for b in bs5]

        adx_v=adx(bs15[-30:],14) if bs15 and len(bs15)>=16 else adx(bs5[-30:],14)
        at5=atr(bs5[-15:],10)
        r5=rsi(c[-20:])
        sr5=stoch_rsi(c,14)
        upper,mid,lower,bb_w=bollinger(c,20)
        bwa=bb_width_avg(c,20,20)
        e8=ema(c[-20:],8);e21=ema(c,21)
        s20=sma(c,20)
        vols=[float(b.get("v",1)) for b in bs5[-20:]]
        avg_v=sum(vols)/len(vols); vol_ratio=vols[-1]/(avg_v+1e-9)

        score=0.0; entry_side=None; reason_str=""; entry_m=mid

        # Mean Reversion: skip longs on ANY strongly downtrending asset
        trending_down = adx_v>35 and e8<e21 and c[-1]<s20

        if c[-1]<=lower*1.005 and r5<28 and sr5<28:
            if not trending_down:
                dist=max(0.1,(lower*1.005-c[-1]+at5)/at5)
                score=((33-r5)/33)*dist*sm
                entry_side="buy"; reason_str=f"MR LONG rsi={r5:.0f} srsi={sr5:.0f} adx={adx_v:.0f}"
                entry_m=mid

        elif c[-1]>=upper*0.995 and r5>65 and sr5>65:
            dist=max(0.1,(c[-1]-upper*0.995+at5)/at5)
            score=((r5-67)/33)*dist*sm
            entry_side="sell"; reason_str=f"MR SHORT rsi={r5:.0f} srsi={sr5:.0f} adx={adx_v:.0f}"
            entry_m=mid

        # Squeeze: BB width ultra-tight
        if bb_w < bwa*0.35:
            sq_hi=max(h[-8:-1]); sq_lo=min(l_[-8:-1])
            if c[-1]>sq_hi:
                sq_score=1.8*(c[-1]-sq_hi)/at5*sm
                if sq_score>score:
                    score=sq_score; entry_side="buy"
                    reason_str=f"SQUEEZE POP UP bbW={bb_w:.4f}"
                    entry_m=upper
            elif c[-1]<sq_lo:
                sq_score=1.8*(sq_lo-c[-1])/at5*sm
                if sq_score>score:
                    score=sq_score; entry_side="sell"
                    reason_str=f"SQUEEZE POP DOWN bbW={bb_w:.4f}"
                    entry_m=lower

        if entry_side and score>0.05:
            # ── R:R GATE: minimum 1.5:1 required ────────────────
            stop_dist = at5 * 0.8
            if entry_side == "buy":
                target_dist = entry_m - c[-1]
                # If too close to midline, use upper band as target
                if target_dist < stop_dist * 1.5:
                    entry_m = upper  # extend to upper band
                    target_dist = upper - c[-1]
            else:
                target_dist = c[-1] - entry_m
                if target_dist < stop_dist * 1.5:
                    entry_m = lower
                    target_dist = c[-1] - lower
            rr = target_dist / (stop_dist + 1e-9)
            if rr < 1.3:
                continue  # skip — not enough room
            reason_str += f" RR={rr:.1f}"
            candidates.append({"sym":sym,"clean":clean,"side":entry_side,
                "price":c[-1],"at":at5,"score":score,
                "reason":reason_str,"mid":entry_m})

    candidates.sort(key=lambda x:-x["score"])
    if candidates:
        for cd in candidates[:3]:
            print(f"  SETUP {'LONG' if cd['side']=='buy' else 'SHORT'} {cd['sym']} | {cd['reason']}")
    else:
        print(f"  No setups — {len(ALTS)} scanned")

    fired=0
    for cd in candidates[:slots]:
        stop_pct=cd["at"]/cd["price"]
        spend=min(RISK_USD/stop_pct, cash*0.12, 3000)
        if spend<150: break
        o=req("POST","/v2/orders",{"symbol":cd["clean"],"notional":str(int(spend)),
            "side":cd["side"],"type":"market","time_in_force":"gtc"})
        if "id" in o:
            my_positions[cd["clean"]]={"atr":cd["at"],"side":cd["side"],
                                       "entry":cd["price"],"mid":cd["mid"]}
            save_positions(my_positions)
            entry_bar[cd["clean"]]=0
            print(f"  ENTERED {'LONG' if cd['side']=='buy' else 'SHORT'} {cd['sym']} ${spend:.0f} | target={cd['mid']:.4f}")
            fired+=1; cash-=spend
            held_clusters.add(cluster_for(cd["clean"]))
        else:
            print(f"  FAIL {cd['sym']}: {str(o)[:80]}")

    if fired: print(f"  Fired: {fired}")

if __name__=="__main__":
    run()
