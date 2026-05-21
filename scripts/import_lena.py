"""
Импорт оценок из файлов «Лене 1.xlsx» и «Лене 2.xlsx»
в чек-лист «ТВК Продление Аудит» (id=9).

Запуск из корня проекта:
    python -m scripts.import_lena
"""
from __future__ import annotations

import html
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

# ── добавляем корень проекта в sys.path ──────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy.orm import Session
from app.database import SessionLocal
from app.models import Checklist, Evaluation, EvaluationItem
from app.scoring import calculate_scores, MONTH_NAMES

# ── константы ────────────────────────────────────────────────────────────────
CHECKLIST_ID = 9
EVALUATOR_ID = 2   # Наумова Екатерина
DEPARTMENT   = "Отдел продления 2"

# Пути к файлам
FILES = [
    ROOT / "Лене 1.xlsx",
    ROOT / "Лене 2.xlsx",
]

# Столбцы критериев: индекс (1-based) → criterion_id
# G(7)→111, I(9)→112 … BI(61)→138
CRIT_VALUE_COLS: dict[int, int] = {7 + i * 2: 111 + i for i in range(28)}
# Комментарий = следующий столбец после значения
CRIT_COMMENT_COLS: dict[int, int] = {col + 1: crit_id for col, crit_id in CRIT_VALUE_COLS.items()}

NS_SS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"

# Паттерн префикса автора в комментарии: «Наумова Екатерина 15/05/2026, 15:17 \n»
_COMMENT_PREFIX = re.compile(
    r'^[А-ЯЁа-яё]+ [А-ЯЁа-яё]+ \d{2}/\d{2}/\d{4}, \d{2}:\d{2}\s*\n',
    re.UNICODE,
)


# ── вспомогательные функции ───────────────────────────────────────────────────

def _col_letter_to_idx(s: str) -> int:
    s = re.sub(r'\d', '', s).upper()
    r = 0
    for c in s:
        r = r * 26 + (ord(c) - ord('A') + 1)
    return r


def _read_xlsx(path: Path) -> list[dict[int, str]]:
    """Читает xlsx через XML (обход бага openpyxl с hyperlink)."""
    with zipfile.ZipFile(path) as z:
        ss_root = ET.fromstring(z.read("xl/sharedStrings.xml"))
        shared: list[str] = []
        for si in ss_root.findall(f"{{{NS_SS}}}si"):
            t_els = si.findall(f".//{{{NS_SS}}}t")
            shared.append(html.unescape("".join(t.text or "" for t in t_els)))

        ws_root = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))
        rows: list[dict[int, str]] = []
        for row_el in ws_root.findall(f".//{{{NS_SS}}}row"):
            cells: dict[int, str] = {}
            for c in row_el.findall(f"{{{NS_SS}}}c"):
                ref = c.get("r", "")
                col_idx = _col_letter_to_idx(re.sub(r'\d', '', ref))
                t_attr = c.get("t", "")
                v_el = c.find(f"{{{NS_SS}}}v")
                val = ""
                if v_el is not None and v_el.text:
                    val = shared[int(v_el.text)] if t_attr == "s" else v_el.text
                cells[col_idx] = val
            if cells:
                rows.append(cells)
    return rows


def _parse_dt(s: str) -> datetime | None:
    """Парсит 'DD/MM/YYYY, HH:MM' или 'DD/MM/YYYY'."""
    s = s.strip()
    for fmt in ("%d/%m/%Y, %H:%M", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    return None


def _extract_deal_id(url: str) -> str | None:
    """Извлекает deal_id из URL Битрикс24."""
    m = re.search(r'/deal/details/(\d+)/', url)
    return m.group(1) if m else None


def _strip_comment_prefix(text: str) -> str:
    """Удаляет prefix «Имя Фамилия DD/MM/YYYY, HH:MM \n» из комментария."""
    return _COMMENT_PREFIX.sub("", text, count=1).strip()


def _clean_operator(name: str) -> str:
    """Убирает скобочные пояснения: «Зайцева Яна (Отпуск …)» → «Зайцева Яна»."""
    return re.sub(r'\s*\(.*?\)', '', name).strip()


def _batch_stages(deal_ids: list[str]) -> dict[str, str]:
    """Запрашивает STAGE_SEMANTIC_ID для списка сделок из Битрикс MySQL."""
    if not deal_ids:
        return {}
    from app.bitrix import _get_connection, SEMANTIC_TO_STAGE, _DEALS_TABLE  # noqa
    stages: dict[str, str] = {}
    try:
        conn = _get_connection()
        try:
            placeholders = ",".join(["%s"] * len(deal_ids))
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT ID, STAGE_SEMANTIC_ID FROM `{_DEALS_TABLE}` WHERE ID IN ({placeholders})",
                    [int(d) for d in deal_ids],
                )
                for row in cur.fetchall():
                    sem = row.get("STAGE_SEMANTIC_ID") or "P"
                    stages[str(row["ID"])] = SEMANTIC_TO_STAGE.get(sem, "в работе")
        finally:
            conn.close()
    except Exception as exc:
        print(f"  [WARN] Не удалось получить стадии из Битрикс: {exc}")
    return stages


# ── основная логика ───────────────────────────────────────────────────────────

def parse_rows(path: Path) -> list[dict]:
    """Возвращает список словарей с данными для каждой оценки."""
    xlsx_rows = _read_xlsx(path)
    result = []
    for i, row in enumerate(xlsx_rows):
        if i == 0:
            continue  # заголовок
        deal_url_raw = row.get(4, "").strip()  # col D
        deal_id = _extract_deal_id(deal_url_raw)
        if not deal_id:
            print(f"  [SKIP] row {i+1}: нет deal_id в '{deal_url_raw}'")
            continue

        operator_raw = row.get(2, "").strip()  # col B
        operator = _clean_operator(operator_raw)

        call_date = _parse_dt(row.get(5, ""))   # col E
        eval_date = _parse_dt(row.get(6, ""))   # col F

        # Критерии
        items: list[tuple[int, str, str]] = []
        for col_idx, crit_id in CRIT_VALUE_COLS.items():
            raw_val = row.get(col_idx, "").strip()
            if raw_val == "1":
                value = "yes"
            elif raw_val == "0":
                value = "no"
            else:
                value = "na"

            comment_col = col_idx + 1
            raw_comment = row.get(comment_col, "").strip()
            comment = _strip_comment_prefix(raw_comment) if raw_comment else ""
            items.append((crit_id, value, comment))

        # Общий комментарий
        bl = row.get(64, "").strip()
        bm = row.get(65, "").strip()
        general_comment_parts = [_strip_comment_prefix(p) for p in [bl, bm] if p]
        general_comment = "\n".join(general_comment_parts) or None

        result.append({
            "deal_id":        deal_id,
            "deal_url":       f"https://entera.bitrix24.ru/crm/deal/details/{deal_id}/",
            "operator":       operator,
            "department":     row.get(3, "").strip() or DEPARTMENT,
            "call_date":      call_date,
            "eval_date":      eval_date,
            "items":          items,
            "general_comment": general_comment,
        })
    return result


def run():
    db: Session = SessionLocal()
    try:
        checklist = db.query(Checklist).filter(Checklist.id == CHECKLIST_ID).first()
        if not checklist:
            print(f"Чек-лист #{CHECKLIST_ID} не найден")
            return

        print(f"Чек-лист: {checklist.name} (id={checklist.id})")

        # Собираем все строки из обоих файлов
        all_rows: list[dict] = []
        for f in FILES:
            if not f.exists():
                print(f"[ERROR] Файл не найден: {f}")
                continue
            rows = parse_rows(f)
            print(f"{f.name}: {len(rows)} строк")
            all_rows.extend(rows)

        print(f"Итого строк: {len(all_rows)}")

        # Получаем стадии из Битрикс одним запросом
        deal_ids = list({r["deal_id"] for r in all_rows})
        print(f"Уникальных сделок: {len(deal_ids)}, запрашиваем стадии из Битрикс...")
        stages = _batch_stages(deal_ids)
        missing = [d for d in deal_ids if d not in stages]
        if missing:
            print(f"  [WARN] Стадии не найдены для сделок: {missing} → будет 'в работе'")

        # Набор уже опубликованных deal_id для этого чек-листа (до импорта)
        published_deals: set[str] = set(
            r[0] for r in
            db.query(Evaluation.deal_id)
            .filter(
                Evaluation.checklist_id == CHECKLIST_ID,
                Evaluation.status == "published",
                Evaluation.deal_id.isnot(None),
            )
            .all()
        )

        imported = 0
        drafts = 0

        for row in all_rows:
            deal_id = row["deal_id"]
            stage = stages.get(deal_id, "в работе")
            call_dt: datetime | None = row["call_date"]
            eval_dt: datetime | None = row["eval_date"]

            # Определяем статус: если deal_id уже опубликован в этом чек-листе → черновик
            if deal_id in published_deals:
                status = "draft"
                drafts += 1
            else:
                status = "published"
                published_deals.add(deal_id)

            # Метаданные времени
            week_num = call_dt.isocalendar()[1] if call_dt else None
            week_year = call_dt.isocalendar()[0] if call_dt else None
            month = MONTH_NAMES[call_dt.month - 1] if call_dt else None

            # Считаем баллы
            total_score, _ = calculate_scores(row["items"], checklist)

            ev = Evaluation(
                checklist_id=CHECKLIST_ID,
                deal_id=deal_id,
                deal_url=row["deal_url"],
                operator_name=row["operator"],
                department=row["department"],
                eval_date=call_dt,
                week_num=week_num,
                week_year=week_year,
                month=month,
                stage=stage,
                total_score=total_score,
                evaluator_id=EVALUATOR_ID,
                general_comment=row["general_comment"],
                status=status,
                created_at=eval_dt or datetime.utcnow(),
                updated_at=eval_dt,
            )
            db.add(ev)
            db.flush()  # получаем ev.id

            for crit_id, value, comment in row["items"]:
                db.add(EvaluationItem(
                    evaluation_id=ev.id,
                    criterion_id=crit_id,
                    value=value,
                    comment=comment or None,
                ))

            imported += 1
            flag = " [DRAFT]" if status == "draft" else ""
            print(f"  + #{deal_id} {row['operator']} {total_score:.1f}% {stage}{flag}")

        db.commit()
        print(f"\nГотово: {imported} оценок сохранено ({drafts} черновик(ов))")

    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    run()
