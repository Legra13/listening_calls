# -*- coding: utf-8 -*-
"""Собирает автономный HTML-файл дашборда: страница + данные + d3 в одном файле.

Файл открывается двойным кликом, без интернета и без claude.ai.

    python3 scripts/build_standalone_dashboard.py <page.html> <csv-dir> <d3.min.js> <out.html>

Страница написана под дашборд-артефакт: она ждёт объект `dash` и CSS-переменные
темы от хоста. Здесь и то и другое подставляется заглушками, сама разметка и
отрисовка не меняются — чтобы обе версии оставались одной и той же страницей.
"""
import os
import re
import sys

ARGS = sys.argv[1:]
if len(ARGS) != 4:
    sys.exit(__doc__)
PAGE, CSV_DIR, D3, OUT = ARGS

DATASETS = ['dynamics', 'employees', 'emp_blocks', 'stats', 'bins',
            'scores', 'departments', 'impact', 'cohort']

# ── Токены темы: на сайте их даёт хост, здесь задаём сами ────────────────────
TOKENS = """
:root {
  color-scheme: light;
  --font-anthropic-sans: -apple-system, BlinkMacSystemFont, "Segoe UI", "Inter", "Helvetica Neue", Arial, sans-serif;
  --font-anthropic-serif: "Iowan Old Style", "Palatino Linotype", Palatino, Georgia, "Times New Roman", serif;

  --cds-font-size-caption: 12px;
  --cds-font-size-body: 14px;
  --cds-font-size-heading: 16px;
  --cds-font-size-title: 22px;
  --cds-font-weight-medium: 600;

  --cds-gap-xs: 4px;  --cds-gap-sm: 8px;  --cds-gap-md: 14px;  --cds-gap-lg: 20px;  --cds-gap-xl: 32px;
  --cds-pad-xs: 4px;  --cds-pad-sm: 8px;  --cds-pad-md: 12px;  --cds-pad-lg: 18px;  --cds-pad-xl: 24px;
  --cds-radius: 10px;
  --cds-dur-slow: 240ms;

  --color-bg: #fbfaf8;
  --color-panel: #ffffff;
  --color-fg: #1c1b19;
  --color-fg-muted: #6c6a65;
  --color-border-line: #e6e3de;
  --color-ok: #2e7d4f;
  --color-warn: #b8791a;
  --color-bad: #c0392b;

  --cds-chart-grid: #eeebe6;
  --cds-chart-axis: #c8c5be;
  --cds-chart-reference: #a8a59e;
  --cds-chart-muted: #c3c0b9;
  --cds-chart-status-critical: #c0392b;
  --cds-surface-popover: #ffffff;
  --cds-border: #ddd9d3;
  --cds-shadow-popover: 0 6px 20px rgba(28, 27, 25, .14);
  --cds-text-secondary: #6c6a65;
}

@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --color-bg: #191917;
    --color-panel: #232220;
    --color-fg: #ecebe8;
    --color-fg-muted: #9e9c96;
    --color-border-line: #353430;
    --color-ok: #5fbf85;
    --color-warn: #d9a441;
    --color-bad: #e2705f;
    --cds-chart-grid: #2d2c29;
    --cds-chart-axis: #4a4843;
    --cds-chart-reference: #6f6c65;
    --cds-chart-muted: #55534d;
    --cds-chart-status-critical: #e2705f;
    --cds-surface-popover: #2b2a27;
    --cds-border: #3d3b36;
    --cds-shadow-popover: 0 6px 20px rgba(0, 0, 0, .45);
    --cds-text-secondary: #9e9c96;
  }
}

:root[data-theme="dark"] {
  color-scheme: dark;
  --color-bg: #191917;
  --color-panel: #232220;
  --color-fg: #ecebe8;
  --color-fg-muted: #9e9c96;
  --color-border-line: #353430;
  --color-ok: #5fbf85;
  --color-warn: #d9a441;
  --color-bad: #e2705f;
  --cds-chart-grid: #2d2c29;
  --cds-chart-axis: #4a4843;
  --cds-chart-reference: #6f6c65;
  --cds-chart-muted: #55534d;
  --cds-chart-status-critical: #e2705f;
  --cds-surface-popover: #2b2a27;
  --cds-border: #3d3b36;
  --cds-shadow-popover: 0 6px 20px rgba(0, 0, 0, .45);
  --cds-text-secondary: #9e9c96;
}

html, body { margin: 0; }
body {
  background: var(--color-bg);
  color: var(--color-fg);
  font: var(--cds-font-size-body)/1.5 var(--font-anthropic-sans);
  -webkit-text-size-adjust: 100%;
}
.sa-wrap { max-width: 1120px; margin-inline: auto; padding-block: 28px 64px; padding-inline: 20px; }
.sa-foot { max-width: 1120px; margin-inline: auto; padding-inline: 20px; padding-block: 0 40px;
           font-size: var(--cds-font-size-caption); color: var(--color-fg-muted); }
img { max-width: 100%; }
[hidden] { display: none !important; }
"""

# ── Заглушка `dash`: на сайте её даёт тип артефакта ──────────────────────────
SHIM = """
(() => {
  const parseCSV = (text) => {
    const rows = [];
    let row = [], field = '', quoted = false;
    for (let i = 0; i < text.length; i += 1) {
      const c = text[i];
      if (quoted) {
        if (c === '"') {
          if (text[i + 1] === '"') { field += '"'; i += 1; } else quoted = false;
        } else field += c;
      } else if (c === '"') quoted = true;
      else if (c === ',') { row.push(field); field = ''; }
      else if (c === '\\n') { row.push(field); field = ''; rows.push(row); row = []; }
      else if (c !== '\\r') field += c;
    }
    if (field !== '' || row.length) { row.push(field); rows.push(row); }
    const head = rows.shift() || [];
    return {
      columns: head,
      data: rows.filter((r) => r.length === head.length)
                .map((r) => Object.fromEntries(head.map((h, i) => [h, r[i]]))),
    };
  };

  const store = {};
  document.querySelectorAll('script[type="text/csv"][data-id]').forEach((tag) => {
    store[tag.dataset.id] = parseCSV(tag.textContent.replace(/^\\n/, ''));
  });

  const drawers = new Map();
  const visible = (el) => !el.closest('[hidden]');

  const run = (name) => {
    const fn = drawers.get(name);
    if (!fn) return;
    document.querySelectorAll('[data-draw="' + name + '"]').forEach((el) => {
      if (visible(el)) {
        try { fn(el); } catch (err) { console.error('Ошибка отрисовки «' + name + '»', err); }
      }
    });
  };
  const runAll = () => drawers.forEach((_, name) => run(name));

  window.dash = {
    data(id) {
      const d = store[id];
      if (!d) return { status: 'missing', data: [], columns: [], message: 'Нет такого набора данных' };
      return { status: 'ok', data: d.data, columns: d.columns, refreshing: false, meta: [] };
    },
    draw(name, fn) { if (fn) drawers.set(name, fn); run(name); },
    onData(fn) { fn(); },
    calc() {}, loader() {}, refresh() {},
    params: () => ({}), setParams() {}, resetParams() {}, setLink() {},
    colors: ['#3b7dd8', '#d4604a', '#2f9e6b', '#a97bd0', '#cf9226', '#3fa9bd', '#c2548f', '#7d8a99'],
  };

  // На сайте скрытую часть страницы отрисовывает сам дашборд, когда её
  // раскрывают. Здесь это делаем сами: после клика по «Показать …».
  document.addEventListener('click', (ev) => {
    if (ev.target.closest('.rp-more')) setTimeout(runAll, 0);
  });

  let t = null;
  let w = window.innerWidth;
  window.addEventListener('resize', () => {
    if (window.innerWidth === w) return;
    w = window.innerWidth;
    clearTimeout(t);
    t = setTimeout(runAll, 150);
  });
})();
"""


def main():
    page = open(PAGE, encoding='utf-8').read()

    style = re.search(r'<style>(.*?)</style>', page, re.S)
    script = re.search(r'<script>(.*?)</script>\s*$', page, re.S)
    if not style or not script:
        sys.exit('Не нашёл <style> или завершающий <script> в странице')
    body = page[style.end():script.start()]
    body = re.sub(r'<script[^>]*>.*?</script>', '', body, flags=re.S).strip()

    csv_tags = []
    for name in DATASETS:
        path = os.path.join(CSV_DIR, name + '.csv')
        text = open(path, encoding='utf-8').read()
        if '</script' in text.lower():
            sys.exit(f'{name}.csv содержит закрывающий тег script — встраивать нельзя')
        csv_tags.append(f'<script type="text/csv" data-id="{name}">\n{text}</script>')

    d3_src = open(D3, encoding='utf-8').read()

    out = f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Прослушка продлений ТВК</title>
<style>{TOKENS}
{style.group(1)}</style>
</head>
<body>
<div class="sa-wrap">
{body}
</div>
<p class="sa-foot">Автономная копия отчёта: данные вшиты в файл, интернет не нужен.
Содержит фамилии сотрудников и их оценки — не выкладывайте файл в общий доступ.</p>

{chr(10).join(csv_tags)}

<script>{d3_src}</script>
<script>{SHIM}</script>
<script>{script.group(1)}</script>
</body>
</html>
"""
    with open(OUT, 'w', encoding='utf-8') as f:
        f.write(out)
    print(f'Готово: {OUT}')
    print(f'  размер: {os.path.getsize(OUT) / 1024 / 1024:.2f} МБ')
    print(f'  наборов данных: {len(csv_tags)}')


if __name__ == '__main__':
    main()
