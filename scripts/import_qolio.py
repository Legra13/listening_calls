"""
Импорт исторических данных из выгрузки Qolio.xlsx в БД приложения.
Запуск: python3 scripts/import_qolio.py <путь_к_файлу.xlsx>
"""
import sys, re
sys.path.insert(0, '.')

# ── патч openpyxl (файл содержит нестандартные гиперссылки) ──────────────────
import openpyxl.worksheet.hyperlink as _hl
_orig_hl_init = _hl.Hyperlink.__init__
def _safe_hl_init(self, *args, **kwargs):
    kwargs.pop('address', None)
    _orig_hl_init(self, *args, **kwargs)
_hl.Hyperlink.__init__ = _safe_hl_init
# ─────────────────────────────────────────────────────────────────────────────

import openpyxl
from datetime import datetime
from app.database import SessionLocal
from app.models import (
    Checklist, Block, Criterion, Evaluation, EvaluationItem, User, DealCache
)
from app.scoring import calculate_scores, MONTH_NAMES
from sqlalchemy.orm import joinedload

XLSX_PATH = sys.argv[1] if len(sys.argv) > 1 else "Выгрузка по сотрудникам Qolio.xlsx"

# ── Позиционное соответствие колонок Excel → criterion_id в БД ───────────────
# Критерии идут через одну (каждая чётная — значение, следующая — комментарий)
# Индексы колонок Excel (0-based): 6, 8, 10, 12, 14 ... → criterion_id 1, 2, 3 ...
CRITERIA_COL_START = 6   # первая колонка с критерием
CRITERIA_COL_STEP  = 2   # шаг (критерий + комментарий)
CRITERIA_COUNT     = 34  # всего критериев


def parse_date(s) -> datetime | None:
    if not s:
        return None
    if isinstance(s, datetime):
        return s
    s = str(s).strip()
    for fmt in ("%d/%m/%Y, %H:%M", "%d/%m/%Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    return None


def extract_deal_id(raw) -> str | None:
    if not raw:
        return None
    s = str(raw)
    # https://entera.bitrix24.ru/crm/deal/details/1351498/...
    m = re.search(r'/deal/details/(\d+)/', s)
    if m:
        return m.group(1)
    # просто число
    if s.strip().isdigit():
        return s.strip()
    return None


def map_value(v) -> str:
    if v == "" or v is None:
        return "na"
    try:
        iv = int(float(v))
        return "yes" if iv == 1 else "no"
    except (ValueError, TypeError):
        return "na"


def main():
    import warnings; warnings.filterwarnings("ignore")

    print(f"Читаю файл: {XLSX_PATH}")
    wb = openpyxl.load_workbook(XLSX_PATH, data_only=True)
    ws = wb.active
    all_rows = list(ws.values)
    headers = all_rows[0]
    data_rows = all_rows[1:]
    print(f"  Строк данных: {len(data_rows)}")

    db = SessionLocal()

    # ── Загружаем чек-лист ТВК ───────────────────────────────────────────────
    cl = (
        db.query(Checklist)
        .options(joinedload(Checklist.blocks).joinedload(Block.criteria))
        .filter(Checklist.name == "ТВК")
        .first()
    )
    if not cl:
        print("ОШИБКА: чек-лист 'ТВК' не найден в БД")
        sys.exit(1)

    # Строим плоский список criterion_id в порядке чек-листа
    criteria_ids = []
    for bl in sorted(cl.blocks, key=lambda b: b.id):
        for cr in sorted(bl.criteria, key=lambda c: c.id):
            criteria_ids.append(cr.id)
    print(f"  Критериев в чек-листе: {len(criteria_ids)}")

    if len(criteria_ids) != CRITERIA_COUNT:
        print(f"  ПРЕДУПРЕЖДЕНИЕ: ожидалось {CRITERIA_COUNT}, в БД {len(criteria_ids)}")

    # ── Оценщик — первый admin ────────────────────────────────────────────────
    evaluator = db.query(User).first()
    print(f"  Оценщик: {evaluator.username}")

    # ── deal_cache для стадий ─────────────────────────────────────────────────
    deal_stages = {
        str(dc.deal_id): dc.stage
        for dc in db.query(DealCache).all()
    }

    # ── Импорт ───────────────────────────────────────────────────────────────
    imported = skipped = 0

    for i, row in enumerate(data_rows, start=2):
        operator = str(row[1] or "").strip()
        department = str(row[2] or "").strip()
        phone_or_url = row[3]
        date_comm = parse_date(row[4])
        date_eval = parse_date(row[5])
        itog_raw = row[74]            # колонка "Итог"
        general_comment = str(row[75] or "").strip()

        if not operator:
            skipped += 1
            continue

        deal_id = extract_deal_id(phone_or_url)
        stage = deal_stages.get(deal_id, "в работе") if deal_id else "в работе"

        eval_date = date_comm or date_eval

        # собираем значения критериев
        items_raw: list[tuple[int, str, str]] = []
        for k in range(CRITERIA_COUNT):
            col_val = CRITERIA_COL_START + k * CRITERIA_COL_STEP
            col_cmt = col_val + 1
            val = map_value(row[col_val] if col_val < len(row) else None)
            cmt = str(row[col_cmt] if col_cmt < len(row) and row[col_cmt] else "").strip()
            crit_id = criteria_ids[k] if k < len(criteria_ids) else None
            if crit_id:
                items_raw.append((crit_id, val, cmt))

        total_score, _ = calculate_scores(items_raw, cl)

        ev = Evaluation(
            checklist_id=cl.id,
            deal_id=deal_id,
            operator_name=operator,
            department=department or None,
            eval_date=eval_date,
            week_num=eval_date.isocalendar()[1] if eval_date else None,
            week_year=eval_date.year if eval_date else None,
            month=MONTH_NAMES[eval_date.month - 1] if eval_date else None,
            stage=stage,
            total_score=total_score,
            evaluator_id=evaluator.id,
            general_comment=general_comment or None,
        )
        db.add(ev)
        db.flush()

        for crit_id, val, cmt in items_raw:
            db.add(EvaluationItem(
                evaluation_id=ev.id,
                criterion_id=crit_id,
                value=val,
                comment=cmt or None,
            ))

        imported += 1
        if imported % 100 == 0:
            print(f"  ... {imported} строк импортировано")

    db.commit()
    print(f"\nГотово: импортировано {imported}, пропущено {skipped}")


if __name__ == "__main__":
    main()
