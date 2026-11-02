"""
Связь качества звонка с исходом сделки — итоговый анализ проекта прослушки.

Отвечает на вопрос: предсказывает ли балл звонка, закроется ли сделка успешно.
Считает по опубликованным оценкам из data/app.db; стадия сделки берётся из
поля evaluations.stage (его актуальность поддерживает scripts/sync_deal_stages.py).

Запуск:  python3 scripts/analyze_quality_vs_outcome.py

Результат на 02.11.2026: связи нет (r = +0.043, p = 0.50). Причина — Win Rate
92.8% при 18 провалах на 249 закрытых сделок: в исходе почти нет разброса,
который балл мог бы объяснить. Подробности — в разделе CLAUDE.md
«Итог проекта прослушки».
"""
import math, os, sqlite3, sys
from collections import defaultdict

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "app.db")
con = sqlite3.connect(DB); con.row_factory = sqlite3.Row

WON, LOST, WIP = "сделка успешна", "не смог продать", "в работе"

# --- helpers ---------------------------------------------------------------
def mean(xs): return sum(xs)/len(xs) if xs else None

def pearson(x, y):
    n = len(x)
    if n < 3: return None
    mx, my = mean(x), mean(y)
    sxy = sum((a-mx)*(b-my) for a, b in zip(x, y))
    sxx = sum((a-mx)**2 for a in x); syy = sum((b-my)**2 for b in y)
    if sxx <= 0 or syy <= 0: return None
    return sxy/math.sqrt(sxx*syy)

def _betacf(a, b, x):
    MAXIT, EPS, FPMIN = 200, 3e-12, 1e-300
    qab, qap, qam = a+b, a+1.0, a-1.0
    c = 1.0; d = 1.0 - qab*x/qap
    if abs(d) < FPMIN: d = FPMIN
    d = 1.0/d; h = d
    for m in range(1, MAXIT+1):
        m2 = 2*m
        aa = m*(b-m)*x/((qam+m2)*(a+m2))
        d = 1.0+aa*d; d = FPMIN if abs(d) < FPMIN else d
        c = 1.0+aa/c; c = FPMIN if abs(c) < FPMIN else c
        d = 1.0/d; h *= d*c
        aa = -(a+m)*(qab+m)*x/((a+m2)*(qap+m2))
        d = 1.0+aa*d; d = FPMIN if abs(d) < FPMIN else d
        c = 1.0+aa/c; c = FPMIN if abs(c) < FPMIN else c
        d = 1.0/d; de = d*c; h *= de
        if abs(de-1.0) < EPS: break
    return h

def betainc(a, b, x):
    if x <= 0: return 0.0
    if x >= 1: return 1.0
    lb = (math.lgamma(a+b)-math.lgamma(a)-math.lgamma(b)
          + a*math.log(x) + b*math.log(1.0-x))
    if x < (a+1.0)/(a+b+2.0):
        return math.exp(lb)*_betacf(a, b, x)/a
    return 1.0 - math.exp(lb)*_betacf(b, a, 1.0-x)/b

def t_pvalue(t, df):
    """двусторонний p для t-статистики"""
    if df <= 0: return None
    return betainc(df/2.0, 0.5, df/(df+t*t))

def r_pvalue(r, n):
    if r is None or n < 3: return None
    if abs(r) >= 1: return 0.0
    t = r*math.sqrt((n-2)/(1-r*r))
    return t_pvalue(t, n-2)

def fisher_exact(a, b, c, d):
    """2x2: [[a,b],[c,d]], двусторонний p"""
    n = a+b+c+d
    def lhyp(a_):
        b_ = (a+b)-a_; c_ = (a+c)-a_; d_ = n-a_-b_-c_
        if min(a_, b_, c_, d_) < 0: return None
        return (math.lgamma(a+b+1)+math.lgamma(c+d+1)+math.lgamma(a+c+1)+math.lgamma(b+d+1)
                - math.lgamma(n+1)-math.lgamma(a_+1)-math.lgamma(b_+1)-math.lgamma(c_+1)-math.lgamma(d_+1))
    p0 = lhyp(a)
    if p0 is None: return None
    tot = 0.0
    for a_ in range(0, min(a+b, a+c)+1):
        p = lhyp(a_)
        if p is not None and p <= p0 + 1e-9:
            tot += math.exp(p)
    return min(1.0, tot)

def welch(x, y):
    """Welch t-test -> (t, df, p)"""
    nx, ny = len(x), len(y)
    if nx < 2 or ny < 2: return None
    mx, my = mean(x), mean(y)
    vx = sum((a-mx)**2 for a in x)/(nx-1)
    vy = sum((a-my)**2 for a in y)/(ny-1)
    se2 = vx/nx + vy/ny
    if se2 <= 0: return None
    t = (mx-my)/math.sqrt(se2)
    df = se2**2 / ((vx/nx)**2/(nx-1) + (vy/ny)**2/(ny-1))
    return t, df, t_pvalue(t, df)

# --- load ------------------------------------------------------------------
checklists = {r["id"]: dict(r) for r in con.execute("SELECT * FROM checklists")}
blocks = defaultdict(list)
for r in con.execute("SELECT * FROM blocks ORDER BY checklist_id, order_index"):
    blocks[r["checklist_id"]].append(dict(r))
crits = defaultdict(list)
crit_by_id = {}
for r in con.execute("SELECT * FROM criteria ORDER BY block_id, order_index"):
    d = dict(r); crits[r["block_id"]].append(d); crit_by_id[r["id"]] = d

evs = [dict(r) for r in con.execute(
    "SELECT * FROM evaluations WHERE status='published' ORDER BY id")]
items = defaultdict(dict)
for r in con.execute("SELECT evaluation_id, criterion_id, value FROM evaluation_items"):
    items[r["evaluation_id"]][r["criterion_id"]] = r["value"]
close = {r["deal_id"]: r["close_date"] for r in con.execute(
    "SELECT deal_id, close_date FROM deal_cache")}

def block_scores(ev):
    out = {}
    vals = items[ev["id"]]
    for bl in blocks[ev["checklist_id"]]:
        num = den = 0.0
        for cr in crits[bl["id"]]:
            v = vals.get(cr["id"], "na")
            if v == "na": continue
            den += cr["weight"]
            if cr["score_type"] == "range":
                try: num += (int(v)/(cr["score_max"] or 5))*cr["weight"]
                except (ValueError, TypeError): den -= cr["weight"]
            elif v == "yes":
                num += cr["weight"]
        out[bl["id"]] = round(num/den*100, 1) if den > 0 else None
    return out

for ev in evs:
    ev["bs"] = block_scores(ev)
    ev["close_date"] = close.get(ev["deal_id"])

print(f"Всего опубликованных оценок: {len(evs)}")


from datetime import datetime

def pdate(s):
    if not s:
        return None
    s = str(s).split(".")[0]
    for f in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, f)
        except ValueError:
            pass
    return None

# --- 1. общая картина ------------------------------------------------------
def report(sub, title):
    won = [e for e in sub if e["stage"] == WON]
    lost = [e for e in sub if e["stage"] == LOST]
    wip = [e for e in sub if e["stage"] == WIP]
    closed = won + lost
    print(f"\n{'='*78}\n{title}")
    print(f"  оценок: {len(sub)} | успешных: {len(won)} | провал: {len(lost)} | в работе: {len(wip)}")
    if not closed: return
    print(f"  Win Rate (по закрытым): {len(won)/len(closed)*100:.1f}%")
    sw = [e["total_score"] for e in won if e["total_score"] is not None]
    sl = [e["total_score"] for e in lost if e["total_score"] is not None]
    swip = [e["total_score"] for e in wip if e["total_score"] is not None]
    print(f"  Средний балл: успешные {mean(sw):.1f} | провал {mean(sl):.1f} "
          f"| Δ {mean(sw)-mean(sl):+.1f} | в работе {mean(swip) if swip else float('nan'):.1f}")
    w = welch(sw, sl)
    if w: print(f"  t-тест (Welch): t={w[0]:.2f}, df={w[1]:.1f}, p={w[2]:.3f}")
    x = [e["total_score"] for e in closed if e["total_score"] is not None]
    y = [1.0 if e["stage"] == WON else 0.0 for e in closed if e["total_score"] is not None]
    r = pearson(x, y)
    if r is not None:
        print(f"  Корреляция балл↔успех (point-biserial): r={r:+.3f}, p={r_pvalue(r,len(x)):.3f}, n={len(x)}")
    # бакеты
    buckets = [(0,50,"<50"),(50,60,"50–60"),(60,70,"60–70"),(70,80,"70–80"),(80,101,"80+")]
    print(f"  {'Диапазон':>8} {'закрытых':>9} {'успешно':>8} {'провал':>7} {'Win Rate':>9}")
    for lo, hi, lbl in buckets:
        b = [e for e in closed if e["total_score"] is not None and lo <= e["total_score"] < hi]
        if not b: continue
        bw = sum(1 for e in b if e["stage"] == WON)
        print(f"  {lbl:>8} {len(b):>9} {bw:>8} {len(b)-bw:>7} {bw/len(b)*100:>8.1f}%")

report(evs, "ВСЕ ОЦЕНКИ (оба чек-листа)")
for cid in (5, 4):
    report([e for e in evs if e["checklist_id"] == cid],
           f"Чек-лист {cid}: {checklists[cid]['name']}")


from datetime import datetime


for e in evs:
    e["d_call"] = pdate(e["eval_date"]); e["d_close"] = pdate(e["close_date"])
    e["days"] = ((e["d_close"]-e["d_call"]).days
                 if e["d_call"] and e["d_close"] and (e["d_close"]-e["d_call"]).days >= 0 else None)

# ── A. срок наблюдения: не «в работе» ли просто свежие сделки ───────────────
print("="*78, "\nA. Когда были звонки — успешные / провал / в работе (по месяцам)")
mon = defaultdict(lambda: [0,0,0])
for e in evs:
    k = e["d_call"].strftime("%Y-%m") if e["d_call"] else "?"
    i = {WON:0, LOST:1, WIP:2}.get(e["stage"])
    if i is not None: mon[k][i] += 1
print(f"  {'месяц':>8} {'успешн':>7} {'провал':>7} {'в работе':>9} {'доля в работе':>14}")
for k in sorted(mon):
    w,l,p = mon[k]; t = w+l+p
    print(f"  {k:>8} {w:>7} {l:>7} {p:>9} {p/t*100:>13.0f}%")

# ── B. альтернативный исход: закрыта успешно vs всё остальное ───────────────
print("\n" + "="*78, "\nB. Альтернативный исход: «успешно закрыта» vs «провал ИЛИ до сих пор в работе»")
for cid, lbl in ((None,"оба чек-листа"), (5,"чек-лист 5"), (4,"чек-лист 4")):
    sub = [e for e in evs if (cid is None or e["checklist_id"]==cid) and e["total_score"] is not None]
    x = [e["total_score"] for e in sub]; y = [1.0 if e["stage"]==WON else 0.0 for e in sub]
    r = pearson(x,y)
    sw = [e["total_score"] for e in sub if e["stage"]==WON]
    so = [e["total_score"] for e in sub if e["stage"]!=WON]
    w = welch(sw, so)
    print(f"  {lbl:>14}: n={len(sub):>3} успех {mean(sw):.1f} vs остальные {mean(so):.1f} "
          f"(Δ{mean(sw)-mean(so):+.1f})  r={r:+.3f} p={r_pvalue(r,len(x)):.3f}"
          + (f"  t-тест p={w[2]:.3f}" if w else ""))

# ── C. скорость закрытия ────────────────────────────────────────────────────
print("\n" + "="*78, "\nC. Балл vs скорость закрытия (дней от звонка до закрытия сделки)")
for stage, nm in ((WON,"успешные"), (LOST,"провальные")):
    sub = [e for e in evs if e["stage"]==stage and e["days"] is not None and e["total_score"] is not None]
    if len(sub) < 5: continue
    x=[e["total_score"] for e in sub]; d=[float(e["days"]) for e in sub]
    r = pearson(x,d)
    print(f"\n  {nm}: n={len(sub)}, медиана {sorted(e['days'] for e in sub)[len(sub)//2]} дн., "
          f"среднее {mean(d):.0f} дн.  r(балл↔дни)={r:+.3f} p={r_pvalue(r,len(x)):.3f}")
    for lo,hi,l2 in [(0,50,"<50"),(50,60,"50–60"),(60,70,"60–70"),(70,80,"70–80"),(80,101,"80+")]:
        b=[e for e in sub if lo<=e["total_score"]<hi]
        if not b: continue
        ds=sorted(e["days"] for e in b)
        print(f"     {l2:>6}: n={len(b):>3}  среднее {mean([float(z) for z in ds]):>5.0f} дн.  медиана {ds[len(ds)//2]:>4} дн.")

# ── D. блоки ────────────────────────────────────────────────────────────────
print("\n" + "="*78, "\nD. Корреляция блоков с исходом (только закрытые сделки)")
for cid in (5,4):
    sub=[e for e in evs if e["checklist_id"]==cid and e["stage"] in (WON,LOST)]
    won=[e for e in sub if e["stage"]==WON]; lost=[e for e in sub if e["stage"]==LOST]
    print(f"\n  ── Чек-лист {cid}: {checklists[cid]['name']} (успешных {len(won)}, провал {len(lost)})")
    print(f"  {'Блок':<42} {'вес':>4} {'успех':>7} {'провал':>7} {'Δ':>7} {'r':>7} {'p':>6}")
    res=[]
    for bl in blocks[cid]:
        aw=[e["bs"][bl["id"]] for e in won if e["bs"].get(bl["id"]) is not None]
        al=[e["bs"][bl["id"]] for e in lost if e["bs"].get(bl["id"]) is not None]
        if len(aw)<3 or len(al)<3: continue
        pair=[(e["bs"][bl["id"]], 1.0 if e["stage"]==WON else 0.0) for e in sub if e["bs"].get(bl["id"]) is not None]
        r=pearson([p[0] for p in pair],[p[1] for p in pair])
        res.append((mean(aw)-mean(al), bl["name"], bl["weight"], mean(aw), mean(al), r, len(pair)))
    for d,nm,wt,aw,al,r,n in sorted(res, key=lambda z:-abs(z[0])):
        print(f"  {nm[:42]:<42} {wt:>4} {aw:>7.1f} {al:>7.1f} {d:>+7.1f} {r:>+7.3f} {r_pvalue(r,n):>6.3f}")


from datetime import datetime
for e in evs:
    e["d_call"]=pdate(e["eval_date"]); e["d_close"]=pdate(e["close_date"])

# E. не падал ли балл со временем (возможный конфаундер для пункта B)
print("E. Средний балл по месяцу звонка (чек-лист 5)")
mon=defaultdict(list)
for e in evs:
    if e["checklist_id"]==5 and e["d_call"] and e["total_score"] is not None:
        mon[e["d_call"].strftime("%Y-%m")].append(e["total_score"])
for k in sorted(mon): print(f"   {k}: n={len(mon[k]):>3} средний балл {mean(mon[k]):.1f}")

# F. доступность close_date
print("\nF. Наличие даты закрытия сделки")
for st in (WON,LOST):
    sub=[e for e in evs if e["stage"]==st]
    have=[e for e in sub if e["d_close"]]
    neg=[e for e in sub if e["d_close"] and e["d_call"] and (e["d_close"]-e["d_call"]).days<0]
    print(f"   {st}: всего {len(sub)}, с датой закрытия {len(have)}, из них закрытие РАНЬШЕ звонка {len(neg)}")
print("   → в deal_cache всего сделок:", con.execute("SELECT COUNT(*) FROM deal_cache").fetchone()[0],
      "| с close_date:", con.execute("SELECT COUNT(*) FROM deal_cache WHERE close_date IS NOT NULL").fetchone()[0])

# G. «вызревшие» когорты: звонки до 01.08 (исход почти у всех определён)
print("\nG. Только «вызревшие» звонки (до 01.08.2026) — там доля «в работе» мала")
mature=[e for e in evs if e["d_call"] and e["d_call"]<datetime(2026,8,1)]
for cid,lbl in ((None,"оба"),(5,"чек-лист 5"),(4,"чек-лист 4")):
    sub=[e for e in mature if (cid is None or e["checklist_id"]==cid) and e["total_score"] is not None]
    cl=[e for e in sub if e["stage"] in (WON,LOST)]
    if len(cl)<20: continue
    x=[e["total_score"] for e in cl]; y=[1.0 if e["stage"]==WON else 0.0 for e in cl]
    r=pearson(x,y)
    sw=[e["total_score"] for e in cl if e["stage"]==WON]; sl=[e["total_score"] for e in cl if e["stage"]==LOST]
    print(f"   {lbl:>11}: закрытых {len(cl)} (провал {len(sl)}), успех {mean(sw):.1f} vs провал {mean(sl):.1f} "
          f"(Δ{mean(sw)-mean(sl):+.1f}) r={r:+.3f} p={r_pvalue(r,len(x)):.3f}")
    # и «успех vs не-успех» на вызревшей когорте
    y2=[1.0 if e["stage"]==WON else 0.0 for e in sub]
    r2=pearson([e["total_score"] for e in sub],y2)
    print(f"{'':>13}  успех vs (провал+в работе): n={len(sub)} r={r2:+.3f} p={r_pvalue(r2,len(sub)):.3f}")

# H. мощность: какую разницу вообще можно заметить при 18 провалах
print("\nH. Статистическая мощность")
import random
random.seed(7)
closed=[e for e in evs if e["stage"] in (WON,LOST) and e["total_score"] is not None]
sw=[e["total_score"] for e in closed if e["stage"]==WON]
sd=math.sqrt(sum((v-mean(sw))**2 for v in sw)/(len(sw)-1))
nl=sum(1 for e in closed if e["stage"]==LOST); nw=len(sw)
print(f"   разброс баллов (SD) = {sd:.1f}; успешных {nw}, провальных {nl}")
for delta in (5,10,15,20):
    hits=0; T=4000
    for _ in range(T):
        a=[random.gauss(mean(sw),sd) for _ in range(nw)]
        b=[random.gauss(mean(sw)-delta,sd) for _ in range(nl)]
        w=welch(a,b)
        if w and w[2]<0.05: hits+=1
    print(f"   истинная разница {delta:>2} баллов → обнаружим с вероятностью {hits/T*100:>4.0f}%")



# I. критерии: Win Rate «выполнено» vs «не выполнено», точный тест Фишера
print("I. Критерии с наибольшим расхождением Win Rate (только бинарные, закрытые сделки)")
allres=[]
for cid in (5,4):
    sub=[e for e in evs if e["checklist_id"]==cid and e["stage"] in (WON,LOST)]
    for bl in blocks[cid]:
        for cr in crits[bl["id"]]:
            if cr["score_type"]=="range": continue
            yes=[e for e in sub if items[e["id"]].get(cr["id"])=="yes"]
            no =[e for e in sub if items[e["id"]].get(cr["id"])=="no"]
            if len(yes)<10 or len(no)<10: continue
            a=sum(1 for e in yes if e["stage"]==WON); b=len(yes)-a
            c=sum(1 for e in no  if e["stage"]==WON); d=len(no)-c
            p=fisher_exact(a,b,c,d)
            allres.append((abs(a/len(yes)-c/len(no)), cid, bl["name"], cr["text"],
                           len(yes), a/len(yes)*100, len(no), c/len(no)*100, p))
print(f"   проверено критериев: {len(allres)}  (порог «значимо» при поправке Бонферрони: p<{0.05/max(len(allres),1):.4f})")
print(f"   {'ЧЛ':>2} {'критерий':<52} {'n да':>5} {'WR да':>6} {'n нет':>6} {'WR нет':>7} {'ΔWR':>6} {'p':>6}")
for d_,cid,bn,ct,ny,wy,nn,wn,p in sorted(allres,key=lambda z:-z[0])[:12]:
    print(f"   {cid:>2} {ct[:52]:<52} {ny:>5} {wy:>5.0f}% {nn:>6} {wn:>6.0f}% {wy-wn:>+5.0f}% {p:>6.3f}")
sig=[z for z in allres if z[-1]<0.05]
print(f"\n   формально p<0.05: {len(sig)} критериев; ожидаемо случайно при {len(allres)} проверках: ~{0.05*len(allres):.1f}")

# J. операторы: разброс среднего балла и Win Rate
print("\n" + "="*78, "\nJ. Сотрудники: балл vs Win Rate (минимум 8 закрытых сделок)")
ops=defaultdict(list)
for e in evs:
    if e["stage"] in (WON,LOST): ops[e["operator_name"]].append(e)
rows=[]
for op,lst in ops.items():
    if len(lst)<8: continue
    w=sum(1 for e in lst if e["stage"]==WON)
    sc=[e["total_score"] for e in lst if e["total_score"] is not None]
    rows.append((mean(sc), w/len(lst)*100, len(lst), op))
print(f"   {'сотрудник':<28} {'закрытых':>9} {'ср.балл':>8} {'Win Rate':>9}")
for sc,wr,n,op in sorted(rows,key=lambda z:-z[0]):
    print(f"   {op[:28]:<28} {n:>9} {sc:>8.1f} {wr:>8.1f}%")
if len(rows)>=4:
    r=pearson([z[0] for z in rows],[z[1] for z in rows])
    print(f"   Корреляция на уровне сотрудников: r={r:+.3f}, p={r_pvalue(r,len(rows)):.3f}, n={len(rows)} чел.")

# K. категория клиента
print("\n" + "="*78, "\nK. Категория клиента")
cat=defaultdict(lambda: [0,0,[]])
for e in evs:
    k=e["client_category"] or "—"
    if e["stage"]==WON: cat[k][0]+=1
    elif e["stage"]==LOST: cat[k][1]+=1
    if e["total_score"] is not None: cat[k][2].append(e["total_score"])
for k in sorted(cat):
    w,l,sc=cat[k]
    wr=f"{w/(w+l)*100:.0f}%" if w+l else "—"
    print(f"   {k:>3}: успешных {w:>3}, провал {l:>2}, WR {wr:>5}, средний балл {mean(sc) if sc else 0:.1f} (n={len(sc)})")
