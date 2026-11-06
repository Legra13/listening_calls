# -*- coding: utf-8 -*-
"""renewal.json -> плоские CSV-таблицы для дашборда."""
import csv, json, os, sys
SRC = sys.argv[1]; OUT = sys.argv[2]
os.makedirs(OUT, exist_ok=True)
d = json.load(open(SRC, encoding='utf-8'))
MONTHS, MLAB = d['months'], d['mlab']
ML = dict(zip(MONTHS, MLAB))
SCOPE_LABEL = {'all': 'Все отделы', 'ОП1': 'ОП1', 'ОП2': 'ОП2', 'ОП3': 'ОП3'}
# Две выборки для всего, что связано с исходом сделки (см. MATURE_BEFORE в export).
SAMPLES = [('corrMature', 'Вызревшие'), ('corr', 'Все звонки')]
BL = {b['i']: b for b in d['blocks']}

def w(name, header, rows):
    p = os.path.join(OUT, name)
    with open(p, 'w', encoding='utf-8', newline='') as f:
        cw = csv.writer(f); cw.writerow(header); cw.writerows(rows)
    print(f'{name}: {len(rows)} строк')

def num(v): return '' if v is None else v

# 1. Динамика: итог / блоки / критерии × срез × месяц
rows = []
for sc, sdata in d['dyn'].items():
    for m in MONTHS:
        md = sdata[m]
        rows.append([SCOPE_LABEL[sc], ML[m], 'Итог', '', 'Итог ТВК', '', num(md['total']), md['n']])
        for bi in range(10):
            b = BL[bi]
            rows.append([SCOPE_LABEL[sc], ML[m], 'Блок', b['name'], b['name'], b['weight'],
                         num(md['blocks'].get(str(bi), md['blocks'].get(bi))), md['n']])
        for b in d['blocks']:
            for c in b['criteria']:
                cv = md['crits'].get(c['key'])
                rows.append([SCOPE_LABEL[sc], ML[m], 'Критерий', b['name'], c['text'], c['weight'],
                             num(cv['v']) if cv else '', cv['n'] if cv else 0])
w('dynamics.csv', ['Срез', 'Месяц', 'Уровень', 'Блок', 'Показатель', 'Вес', 'Балл', 'Оценок'], rows)

# 2. Сотрудники (широкая таблица)
rows = []
for e in d['employees']:
    rows.append([e['name'], e['deptShort'], e['n'], num(e['total'])]
                + [num(e['months'][m]['total']) for m in MONTHS]
                + [e['won'], e['lost'], e['prog'], num(e['wr'])])
w('employees.csv', ['Сотрудник', 'Отдел', 'Оценок', 'Средний балл'] + MLAB
  + ['Успешных', 'Провалов', 'В работе', 'Win Rate'], rows)

# 3. Сотрудники × блоки (за весь период)
rows = []
for e in d['employees']:
    for bi in range(10):
        rows.append([e['name'], e['deptShort'], BL[bi]['name'], BL[bi]['weight'],
                     num(e['blocks'].get(str(bi), e['blocks'].get(bi)))])
w('emp_blocks.csv', ['Сотрудник', 'Отдел', 'Блок', 'Вес', 'Балл'], rows)

# 4. Сводные показатели по срезам
rows = []
for key, lab in SAMPLES:
    for sc in d['scopes']:
        c = d[key][sc]; sd = d['dyn'][sc]
        tot_n = sum(sd[m]['n'] for m in MONTHS)
        avg = round(sum(sd[m]['total'] * sd[m]['n'] for m in MONTHS if sd[m]['n']) / tot_n, 1) if tot_n else None
        for k, v in [('Оценок', c['n']), ('Средний балл', avg), ('Успешных сделок', c['won']),
                     ('Провалов', c['lost']), ('В работе', c['prog']), ('Win Rate', c['wr']),
                     ('Балл при успехе', c['meanWon']), ('Балл при провале', c['meanLost']),
                     ('Разница баллов', c['delta']), ('p (t-тест)', c['p']),
                     ('Корреляция r', c['r']), ('p (корреляция)', c['rp'])]:
            rows.append([lab, SCOPE_LABEL[sc], k, num(v)])
w('stats.csv', ['Выборка', 'Срез', 'Показатель', 'Значение'], rows)

# 5. Win Rate по диапазонам балла
rows = [[lab, SCOPE_LABEL[sc], b['label'], b['n'], b['won'], b['lost'], num(b['wr'])]
        for key, lab in SAMPLES for sc in d['scopes'] for b in d[key][sc]['bins']]
w('bins.csv', ['Выборка', 'Срез', 'Диапазон балла', 'Закрытых сделок', 'Успешных', 'Провалов', 'Win Rate'], rows)

# 6. Отдельные оценки: балл и исход
OUTC = {1: 'Успешна', 0: 'Провал'}
rows = [[lab, SCOPE_LABEL[sc], p[0], OUTC[p[1]]] for key, lab in SAMPLES
        for sc in d['scopes'] if sc != 'all' for p in d[key][sc]['points']]
w('scores.csv', ['Выборка', 'Отдел', 'Балл', 'Исход'], rows)

# 7. Отделы: сравнение (широкая таблица)
rows = []
for key, lab in SAMPLES:
    for sc in d['scopes']:
        c = d[key][sc]
        rows.append([lab, SCOPE_LABEL[sc], c['n'], c['won'], c['lost'], num(c['wr']),
                     num(c['meanWon']), num(c['meanLost']), num(c['delta']), num(c['p']), num(c['r'])])
w('departments.csv', ['Выборка', 'Срез', 'Звонков', 'Успешных', 'Провалов',
                      'Win Rate', 'Балл при успехе', 'Балл при провале', 'Разница', 'p', 'r'], rows)

# 8. Влияние блоков и критериев на исход
rows = []
for key, lab in SAMPLES:
  for b in d[key]['all']['blocks']:
    rows.append([lab, 'Блок', b['name'], b['name'], b['weight'], num(b['avgAll']), num(b['avgWon']),
                 num(b['avgLost']), num(b['delta']), num(b['p']), b['nWon'], b['nLost'],
                 num(b['wrHigh']), num(b['wrLow']), num(b['wrImpact']), '', '',
                 'да' if b['degen'] else 'нет'])
    for c in b['criteria']:
        rows.append([lab, 'Критерий', b['name'], c['text'], c['weight'], '', '', '', '', num(c['p']),
                     c['nYes'], c['nNo'], num(c['wrYes']), num(c['wrNo']), num(c['impact']),
                     num(c['share']), c['nAns'], 'да' if c['degen'] else 'нет'])
w('impact.csv', ['Выборка', 'Уровень', 'Блок', 'Показатель', 'Вес', 'Средний балл', 'Балл при успехе',
                 'Балл при провале', 'Разница', 'p', 'Сделок (успех)', 'Сделок (провал)',
                 'Win Rate выполнено', 'Win Rate не выполнено', 'Влияние на Win Rate',
                 'Доля выполнения', 'Ответов', 'Мало данных'], rows)

# 9. Вызревание когорт: звонки месяца и что стало с их сделками
MN = {'2026-05': 'май', '2026-06': 'июнь', '2026-07': 'июль',
      '2026-08': 'авг', '2026-09': 'сент', '2026-10': 'окт'}
rows = [[MN[c['month']], c['n'], c['won'], c['lost'], c['prog'], num(c['wr']),
         'да' if c['mature'] else 'нет'] for c in d['cohort']]
w('cohort.csv', ['Месяц звонка', 'Звонков', 'Успешных', 'Провальных', 'В работе',
                 'Win Rate', 'Вызрела'], rows)
