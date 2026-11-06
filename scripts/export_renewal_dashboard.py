# -*- coding: utf-8 -*-
"""Выгрузка агрегатов по отделам продления (май–октябрь 2026) для дашборда."""
import json, math, re, sqlite3, sys
from collections import defaultdict

DB = '/home/egerasimchuk/listening_calls/data/app.db'
OUT = sys.argv[1] if len(sys.argv) > 1 else '/tmp/renewal.json'
WON, LOST, PROG, JUNK = 'сделка успешна', 'не смог продать', 'в работе', 'дубль'
MONTHS = ['2026-05', '2026-06', '2026-07', '2026-08', '2026-09', '2026-10']
# Сделка может стать успешной быстро, а провальной — только когда подписка кончилась.
# Поэтому у свежих звонков провалы ещё не наступили, и Win Rate по ним завышен.
# «Вызревшие» = звонки, у которых исход уже успел определиться.
MATURE_BEFORE = '2026-08-01'
MLAB = {'2026-05': 'май', '2026-06': 'июнь', '2026-07': 'июль',
        '2026-08': 'авг', '2026-09': 'сент', '2026-10': 'окт'}

# --- stats -----------------------------------------------------------------
def mean(xs): return sum(xs) / len(xs) if xs else None

def _betacf(a, b, x):
    MAXIT, EPS, FPMIN = 200, 3e-12, 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0; d = 1.0 - qab * x / qap
    if abs(d) < FPMIN: d = FPMIN
    d = 1.0 / d; h = d
    for m in range(1, MAXIT + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d; d = FPMIN if abs(d) < FPMIN else d
        c = 1.0 + aa / c; c = FPMIN if abs(c) < FPMIN else c
        d = 1.0 / d; h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d; d = FPMIN if abs(d) < FPMIN else d
        c = 1.0 + aa / c; c = FPMIN if abs(c) < FPMIN else c
        d = 1.0 / d; de = d * c; h *= de
        if abs(de - 1.0) < EPS: break
    return h

def betainc(a, b, x):
    if x <= 0: return 0.0
    if x >= 1: return 1.0
    lb = (math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
          + a * math.log(x) + b * math.log(1.0 - x))
    if x < (a + 1.0) / (a + b + 2.0):
        return math.exp(lb) * _betacf(a, b, x) / a
    return 1.0 - math.exp(lb) * _betacf(b, a, 1.0 - x) / b

def t_pvalue(t, df):
    if df <= 0: return None
    return betainc(df / 2.0, 0.5, df / (df + t * t))

def welch(x, y):
    nx, ny = len(x), len(y)
    if nx < 2 or ny < 2: return None
    mx, my = mean(x), mean(y)
    vx = sum((a - mx) ** 2 for a in x) / (nx - 1)
    vy = sum((a - my) ** 2 for a in y) / (ny - 1)
    se2 = vx / nx + vy / ny
    if se2 <= 0: return None
    t = (mx - my) / math.sqrt(se2)
    df = se2 ** 2 / ((vx / nx) ** 2 / (nx - 1) + (vy / ny) ** 2 / (ny - 1))
    return t, df, t_pvalue(t, df)

def pearson(x, y):
    n = len(x)
    if n < 3: return None
    mx, my = mean(x), mean(y)
    sxy = sum((a - mx) * (b - my) for a, b in zip(x, y))
    sxx = sum((a - mx) ** 2 for a in x); syy = sum((b - my) ** 2 for b in y)
    if sxx <= 0 or syy <= 0: return None
    return sxy / math.sqrt(sxx * syy)

def r_pvalue(r, n):
    if r is None or n < 3: return None
    if abs(r) >= 1: return 0.0
    t = r * math.sqrt((n - 2) / (1 - r * r))
    return t_pvalue(t, n - 2)

def fisher_exact(a, b, c, d):
    n = a + b + c + d
    def lhyp(a_):
        b_ = (a + b) - a_; c_ = (a + c) - a_; d_ = n - a_ - b_ - c_
        if min(a_, b_, c_, d_) < 0: return None
        return (math.lgamma(a + b + 1) + math.lgamma(c + d + 1) + math.lgamma(a + c + 1)
                + math.lgamma(b + d + 1) - math.lgamma(n + 1) - math.lgamma(a_ + 1)
                - math.lgamma(b_ + 1) - math.lgamma(c_ + 1) - math.lgamma(d_ + 1))
    p0 = lhyp(a)
    if p0 is None: return None
    tot = 0.0
    for a_ in range(0, min(a + b, a + c) + 1):
        p = lhyp(a_)
        if p is not None and p <= p0 + 1e-9:
            tot += math.exp(p)
    return min(1.0, tot)

def rnd(v, k=1): return None if v is None else round(v, k)

# --- load ------------------------------------------------------------------
con = sqlite3.connect(DB); con.row_factory = sqlite3.Row

def norm(s):
    return re.sub(r'\s+', ' ', s.replace('""', '"').strip().lower())

# алиасы: формулировки одного и того же критерия в чек-листах 4 и 5
ALIAS = {
    'попросил время на диагностику': 'попросил время на диагностику и пообещал конкретный результат',
}
def ckey(bi, text):
    t = norm(text); t = ALIAS.get(t, t)
    return f'{bi}|{t}'

blocks_raw = con.execute(
    "select id, checklist_id, order_index, name, weight from blocks "
    "where checklist_id in (4,5) order by checklist_id, order_index").fetchall()
block_by_id = {r['id']: r for r in blocks_raw}
BLOCK_NAME, BLOCK_W = {}, {}
for r in blocks_raw:
    BLOCK_W[r['order_index']] = r['weight']
    if r['checklist_id'] == 4:          # у чек-листа 4 названия без «(баллы не начисляются)»
        BLOCK_NAME[r['order_index']] = r['name']
    BLOCK_NAME.setdefault(r['order_index'], r['name'])

crit_raw = con.execute(
    "select cr.id, cr.text, cr.weight, cr.score_type, cr.score_max, cr.order_index ci, "
    "       b.checklist_id cl, b.order_index bi "
    "from criteria cr join blocks b on b.id = cr.block_id "
    "where b.checklist_id in (4,5) order by b.checklist_id, b.order_index, cr.order_index").fetchall()
CRIT = {}            # crit_id -> dict
CRIT_META = {}       # key -> {text, weight, bi, cls, order}
for r in crit_raw:
    k = ckey(r['bi'], r['text'])
    CRIT[r['id']] = dict(key=k, bi=r['bi'], weight=r['weight'],
                         score_type=r['score_type'], score_max=r['score_max'] or 5)
    m = CRIT_META.setdefault(k, dict(key=k, bi=r['bi'], text=r['text'].replace('""', '"').strip(),
                                     weight=r['weight'], cls=[], order=(r['bi'], r['ci'], r['cl'])))
    m['cls'].append(r['cl'])

evs = con.execute(
    "select id, checklist_id, deal_id, operator_name, department, stage, eval_date, client_category "
    "from evaluations where status='published' and checklist_id in (4,5) "
    "and department like 'Отдел продления%' and eval_date is not null").fetchall()
items = defaultdict(dict)
for r in con.execute(
        "select ei.evaluation_id eid, ei.criterion_id cid, ei.value v from evaluation_items ei "
        "join evaluations e on e.id = ei.evaluation_id "
        "where e.status='published' and e.checklist_id in (4,5)").fetchall():
    items[r['eid']][r['cid']] = r['v']

ROWS = []
skipped = defaultdict(int)
for e in evs:
    m = (e['eval_date'] or '')[:7]
    if m not in MONTHS:
        skipped['вне периода'] += 1; continue
    if e['stage'] == JUNK:
        skipped['дубль'] += 1; continue
    vals = items.get(e['id'], {})
    bnum, bden = defaultdict(float), defaultdict(float)
    cach = {}
    for cid, v in vals.items():
        c = CRIT.get(cid)
        if not c or v == 'na':
            continue
        if c['score_type'] == 'range':
            try: a = int(v) / c['score_max'] * 100
            except (ValueError, TypeError): continue
        else:
            a = 100.0 if v == 'yes' else 0.0
        cach[c['key']] = a
        bnum[c['bi']] += a / 100 * c['weight']; bden[c['bi']] += c['weight']
    bs = {bi: round(bnum[bi] / bden[bi] * 100, 1) for bi in bden if bden[bi] > 0}
    wn = sum(bs[bi] * BLOCK_W[bi] for bi in bs); wd = sum(BLOCK_W[bi] for bi in bs)
    if wd == 0:
        skipped['нет ответов'] += 1; continue
    ROWS.append(dict(id=e['id'], cl=e['checklist_id'], deal=e['deal_id'], op=e['operator_name'],
                     dept=e['department'], stage=e['stage'], month=m, cat=e['client_category'],
                     date=(e['eval_date'] or '')[:10],
                     total=round(wn / wd, 1), blocks=bs, crits=cach))

SHORT = {'Отдел продления 1': 'ОП1', 'Отдел продления 2': 'ОП2', 'Отдел продления 3': 'ОП3'}
DEPTS = [d for d in SHORT if any(r['dept'] == d for r in ROWS)]
SCOPES = [('all', ROWS)] + [(SHORT[d], [r for r in ROWS if r['dept'] == d]) for d in DEPTS]

# --- 1. динамика ------------------------------------------------------------
def dyn(rows):
    out = {}
    for m in MONTHS:
        sub = [r for r in rows if r['month'] == m]
        if not sub:
            out[m] = dict(n=0, total=None, blocks={}, crits={}); continue
        bl, cr = {}, {}
        for bi in range(10):
            vs = [r['blocks'][bi] for r in sub if bi in r['blocks']]
            bl[bi] = rnd(mean(vs)) if vs else None
        for k in CRIT_META:
            vs = [r['crits'][k] for r in sub if k in r['crits']]
            cr[k] = dict(v=rnd(mean(vs)), n=len(vs)) if vs else None
        out[m] = dict(n=len(sub), total=rnd(mean([r['total'] for r in sub])), blocks=bl, crits=cr)
    return out

# --- 2–3. корреляция --------------------------------------------------------
BINS = [('≤50', -1, 50), ('50–60', 50, 60), ('60–70', 60, 70), ('70–80', 70, 80), ('>80', 80, 101)]

def corr(rows):
    closed = [r for r in rows if r['stage'] in (WON, LOST)]
    w = [r['total'] for r in closed if r['stage'] == WON]
    l = [r['total'] for r in closed if r['stage'] == LOST]
    res = dict(n=len(rows), nClosed=len(closed), won=len(w), lost=len(l),
               avg=rnd(mean([r['total'] for r in rows])) if rows else None,
               prog=sum(1 for r in rows if r['stage'] == PROG),
               wr=rnd(len(w) / len(closed) * 100) if closed else None,
               meanWon=rnd(mean(w)), meanLost=rnd(mean(l)),
               delta=rnd(mean(w) - mean(l)) if w and l else None)
    wt = welch(w, l)
    res['p'] = rnd(wt[2], 4) if wt else None
    xs = [r['total'] for r in closed]; ys = [1 if r['stage'] == WON else 0 for r in closed]
    r_ = pearson(xs, ys)
    res['r'] = rnd(r_, 3); res['rp'] = rnd(r_pvalue(r_, len(closed)), 4)
    res['bins'] = []
    for lab, lo, hi in BINS:
        sub = [r for r in closed if lo < r['total'] <= hi]
        nw = sum(1 for r in sub if r['stage'] == WON)
        res['bins'].append(dict(label=lab, n=len(sub), won=nw, lost=len(sub) - nw,
                                wr=rnd(nw / len(sub) * 100) if sub else None))
    res['points'] = [[r['total'], 1 if r['stage'] == WON else 0] for r in closed]
    # блоки
    bl = []
    for bi in range(10):
        pw = [r['blocks'][bi] for r in closed if r['stage'] == WON and bi in r['blocks']]
        pl = [r['blocks'][bi] for r in closed if r['stage'] == LOST and bi in r['blocks']]
        wt = welch(pw, pl)
        hi = [r for r in closed if bi in r['blocks'] and r['blocks'][bi] >= 50]
        lo = [r for r in closed if bi in r['blocks'] and r['blocks'][bi] < 50]
        nwh = sum(1 for r in hi if r['stage'] == WON); nwl = sum(1 for r in lo if r['stage'] == WON)
        wrh = rnd(nwh / len(hi) * 100) if hi else None
        wrl = rnd(nwl / len(lo) * 100) if lo else None
        cov = [r['blocks'][bi] for r in rows if bi in r['blocks']]
        crits = []
        for k, meta in sorted(CRIT_META.items(), key=lambda kv: kv[1]['order']):
            if meta['bi'] != bi: continue
            yes = [r for r in closed if r['crits'].get(k) == 100.0]
            no = [r for r in closed if r['crits'].get(k) == 0.0]
            if not yes and not no: continue
            ny = sum(1 for r in yes if r['stage'] == WON); nn = sum(1 for r in no if r['stage'] == WON)
            wry = rnd(ny / len(yes) * 100) if yes else None
            wrn = rnd(nn / len(no) * 100) if no else None
            allyes = sum(1 for r in rows if r['crits'].get(k) == 100.0)
            allans = sum(1 for r in rows if k in r['crits'])
            crits.append(dict(key=k, text=meta['text'], weight=meta['weight'], cls=sorted(set(meta['cls'])),
                              nYes=len(yes), nNo=len(no), wrYes=wry, wrNo=wrn,
                              impact=rnd(wry - wrn) if (wry is not None and wrn is not None) else None,
                              p=rnd(fisher_exact(ny, len(yes) - ny, nn, len(no) - nn), 4) if (yes and no) else None,
                              share=rnd(allyes / allans * 100) if allans else None, nAns=allans,
                              degen=(len(yes) < 10 or len(no) < 10)))
        # p ненадёжен, если в одной из групп мало наблюдений или нулевая дисперсия
        degen = (len(pl) < 10 or len(pw) < 10
                 or len(set(pl)) <= 1 or len(set(pw)) <= 1)
        bl.append(dict(i=bi, name=BLOCK_NAME[bi], weight=BLOCK_W[bi],
                       avgWon=rnd(mean(pw)), avgLost=rnd(mean(pl)),
                       nWon=len(pw), nLost=len(pl), degen=degen,
                       delta=rnd(mean(pw) - mean(pl)) if pw and pl else None,
                       p=rnd(wt[2], 4) if wt else None,
                       avgAll=rnd(mean(cov)), nHigh=len(hi), nLow=len(lo), wrHigh=wrh, wrLow=wrl,
                       wrImpact=rnd(wrh - wrl) if (wrh is not None and wrl is not None) else None,
                       criteria=crits))
    res['blocks'] = bl
    return res

# --- сотрудники -------------------------------------------------------------
emps = []
for op in sorted({r['op'] for r in ROWS}):
    sub = [r for r in ROWS if r['op'] == op]
    dep = max({r['dept'] for r in sub}, key=lambda d: sum(1 for r in sub if r['dept'] == d))
    months = {}
    for m in MONTHS:
        s = [r for r in sub if r['month'] == m]
        months[m] = dict(n=len(s), total=rnd(mean([r['total'] for r in s]))) if s else dict(n=0, total=None)
    bl = {}
    for bi in range(10):
        vs = [r['blocks'][bi] for r in sub if bi in r['blocks']]
        bl[bi] = rnd(mean(vs)) if vs else None
    cl = sorted({r['cl'] for r in sub})
    nw = sum(1 for r in sub if r['stage'] == WON); nl = sum(1 for r in sub if r['stage'] == LOST)
    emps.append(dict(name=op, dept=dep, deptShort=SHORT[dep], n=len(sub), cls=cl,
                     total=rnd(mean([r['total'] for r in sub])), months=months, blocks=bl,
                     won=nw, lost=nl, prog=sum(1 for r in sub if r['stage'] == PROG),
                     wr=rnd(nw / (nw + nl) * 100) if (nw + nl) else None))
emps.sort(key=lambda e: (e['deptShort'], -e['n']))

cohort = []
for m in MONTHS:
    sub = [r for r in ROWS if r['month'] == m]
    nw = sum(1 for r in sub if r['stage'] == WON)
    nl = sum(1 for r in sub if r['stage'] == LOST)
    cohort.append(dict(month=m, n=len(sub), won=nw, lost=nl,
                       prog=sum(1 for r in sub if r['stage'] == PROG),
                       wr=rnd(nw / (nw + nl) * 100) if (nw + nl) else None,
                       mature=m < MATURE_BEFORE[:7]))

MATURE = [r for r in ROWS if r['date'] < MATURE_BEFORE]
SCOPES_M = [('all', MATURE)] + [(SHORT[d], [r for r in MATURE if r['dept'] == d]) for d in DEPTS]

payload = dict(
    generated='06.11.2026', months=MONTHS, mlab=[MLAB[m] for m in MONTHS],
    departments=[dict(name=d, short=SHORT[d]) for d in DEPTS],
    scopes=[s for s, _ in SCOPES],
    blocks=[dict(i=bi, name=BLOCK_NAME[bi], weight=BLOCK_W[bi],
                 criteria=[dict(key=m['key'], text=m['text'], weight=m['weight'], cls=sorted(set(m['cls'])))
                           for m in sorted(CRIT_META.values(), key=lambda m: m['order']) if m['bi'] == bi])
            for bi in range(10)],
    dyn={name: dyn(rows) for name, rows in SCOPES},
    corr={name: corr(rows) for name, rows in SCOPES},
    corrMature={name: corr(rows) for name, rows in SCOPES_M},
    cohort=cohort, matureBefore=MATURE_BEFORE,
    employees=emps,
    meta=dict(nEval=len(ROWS), nDeals=len({r['deal'] for r in ROWS if r['deal']}),
              nEmp=len(emps), skipped=dict(skipped),
              byCl={cl: sum(1 for r in ROWS if r['cl'] == cl) for cl in (4, 5)},
              deptMonth={SHORT[d]: {m: sum(1 for r in ROWS if r['dept'] == d and r['month'] == m)
                                    for m in MONTHS} for d in DEPTS}),
)
with open(OUT, 'w', encoding='utf-8') as f:
    json.dump(payload, f, ensure_ascii=False, separators=(',', ':'))
print('rows:', len(ROWS), 'skipped:', dict(skipped), 'employees:', len(emps))
print('all:', {k: payload['corr']['all'][k] for k in ('n', 'nClosed', 'won', 'lost', 'prog', 'wr', 'meanWon', 'meanLost', 'delta', 'p', 'r', 'rp')})
print('вызревшие:', {k: payload['corrMature']['all'][k] for k in ('n', 'nClosed', 'won', 'lost', 'wr', 'delta', 'p', 'r')})
print('bins:', [(b['label'], b['n'], b['wr']) for b in payload['corr']['all']['bins']])
print('dyn all:', [(m, payload['dyn']['all'][m]['n'], payload['dyn']['all'][m]['total']) for m in MONTHS])
import os; print('size KB:', round(os.path.getsize(OUT) / 1024, 1))
