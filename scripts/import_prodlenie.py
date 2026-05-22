"""
Импорт оценок «Продление» из Qolio-выгрузок (xlsx).
Запуск: python3 scripts/import_prodlenie.py
"""
import re
import sys
from datetime import datetime

# Патч: openpyxl не принимает атрибут 'address' в Hyperlink, обходим через **kwargs
from openpyxl.worksheet.hyperlink import Hyperlink as _Hyperlink
_orig_hyperlink_init = _Hyperlink.__init__
def _patched_hyperlink_init(self, **kw):
    kw.pop("address", None)
    _orig_hyperlink_init(self, **kw)
_Hyperlink.__init__ = _patched_hyperlink_init

import openpyxl
import pandas as pd


def read_xlsx_safe(fpath: str) -> pd.DataFrame:
    """Читает xlsx обходя баг с гиперссылками старых версий openpyxl."""
    wb = openpyxl.load_workbook(fpath, read_only=True, data_only=True)
    ws = wb.active
    rows = list(ws.values)
    wb.close()
    if not rows:
        return pd.DataFrame()
    headers = [str(h) if h is not None else "" for h in rows[0]]
    data = [list(r) for r in rows[1:]]
    return pd.DataFrame(data, columns=headers)

sys.path.insert(0, "/home/egerasimchuk/Инструмент_прослушки_звонков")
from sqlalchemy.orm import joinedload

from app.database import SessionLocal
from app.models import Block, Checklist, Criterion, DealCache, Evaluation, EvaluationItem
from app.scoring import calculate_scores, MONTH_NAMES
from app.bitrix import get_deal

CHECKLIST_ID = 9          # ТВК Продление Аудит
EVALUATOR_USER_ID = 2     # Наумова Екатерина

FILES = [
    "/home/egerasimchuk/Инструмент_прослушки_звонков/Продление 1.xlsx",
    "/home/egerasimchuk/Инструмент_прослушки_звонков/Продление 2.xlsx",
    "/home/egerasimchuk/Инструмент_прослушки_звонков/Продление 3.xlsx",
]

# Excel column name → DB criterion ID (checklist 9, crits 111-138)
# Столбец «Уточнил количество документов в месяц…» отсутствует в чек-листе — пропускаем
COLUMN_TO_CRIT: dict[str, int] = {
    "Проверил ИНН клиента и связанные компании в интернете": 111,
    "Проанализировал динамику загрузки в Balance_report (если есть)": 112,
    "Использовал правильный заход: говорил об экономии времени, а не о функциях сервиса": 113,
    "Попросил время на диагностику и пообещал конкретный результат": 114,
    "При аномалии в Balance_report — мягко упомянул в разговоре": 115,
    "Корректно отработал возражение «у нас всё хорошо» без давления": 116,
    "Предложил удалённое подключение до начала диагностики": 117,
    "При невозможности установки — предложил альтернативу (RuDesktop, мойассистент.рф)": 118,
    "При отказе от подключения — отработал возражение, проговорил выгоду и только потом перешёл к диагностике устно": 119,
    "Уточнил сферу деятельности и тип компании (внутренняя бухгалтерия или БО)": 120,
    # "Уточнил количество документов в месяц и количество сотрудников" → не в чек-листе, пропуск
    "Открыл калькулятор на ПК клиента через AnyDesk": 121,
    "Выявил все каналы поступления первичных документов и ввёл количество в калькулятор": 122,
    "По ЭДО: уточнил наличие документов реализации": 123,
    "По чекам: если есть — упомянул функцию авансовых отчётов": 124,
    "Настроил только каналы которые назвал клиент": 125,
    "Если есть реализация — установил ЭДО2 на тест и попросил клиента самому отправить документ": 126,
    "Показал клиенту итоговые цифры: экономия в часах и рублях за год, процент рабочего времени, ускорение": 127,
    "Сформировал PDF-отчёт из калькулятора и скачал на ПК клиента": 128,
    "Если клиент уходил в сторонние темы — менеджер вернул разговор в русло диагностики": 129,
    "Если клиент спрашивал «зачем вам это?» — менеджер присоединился и подсветил пользу разговора": 130,
    "Спросил про ошибки при работе с модулем 1С и документы вводимые вручную": 131,
    "Проверил и обновил версию модуля 1С": 132,
    "Проговорил итог аудита с конкретными цифрами из калькулятора": 133,
    "Мягко вышел на тему продления: «подписка заканчивается через 3 месяца, давайте закроем вопрос заранее»": 134,
    "Упомянул комплимент −1 000 ₽ при оплате в течение 5 рабочих дней": 135,
    "Если клиент тестировал ЭДО2 — предложил схему флагман + ЭДО2 со скидкой": 136,
    "Отработал возражения без упоминания конкурентов первым. При возражениях ссылался на расчёт из калькулятора": 137,
    "Договорился о конкретном следующем шаге с датой и внёс данные в CRM": 138,
}


def parse_date(val) -> datetime | None:
    if pd.isna(val) or not str(val).strip():
        return None
    s = str(val).strip()
    for fmt in ("%d/%m/%Y, %H:%M", "%d/%m/%Y %H:%M", "%Y-%m-%d %H:%M:%S", "%d.%m.%Y"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    return None


def extract_deal_id(val) -> str | None:
    if pd.isna(val) or not str(val).strip():
        return None
    m = re.search(r"/deal/details/(\d+)/", str(val))
    return m.group(1) if m else None


def to_value(val) -> str:
    """1/1.0 → yes, 0/0.0 → no, пусто/NaN → na"""
    if pd.isna(val) or str(val).strip() == "":
        return "na"
    try:
        return "yes" if float(val) >= 1 else "no"
    except (ValueError, TypeError):
        return "na"


def clean_comment(val) -> str | None:
    """Убирает префикс «Автор (дата): » из комментария Qolio."""
    if pd.isna(val) or not str(val).strip():
        return None
    text = str(val).strip()
    # Паттерн: «Имя Фамилия (DD.MM.YYYY): текст»
    cleaned = re.sub(r"^[^(]+\(\d{2}\.\d{2}\.\d{4}\):\s*", "", text)
    return cleaned.strip() or None


def main():
    db = SessionLocal()

    # Загружаем чек-лист с блоками и критериями для подсчёта баллов
    checklist = (
        db.query(Checklist)
        .options(joinedload(Checklist.blocks).joinedload(Block.criteria))
        .filter(Checklist.id == CHECKLIST_ID)
        .first()
    )
    if not checklist:
        print(f"Чек-лист {CHECKLIST_ID} не найден!")
        return

    imported = 0
    skipped = 0
    errors = []

    for fpath in FILES:
        fname = fpath.split("/")[-1]
        df = read_xlsx_safe(fpath)
        print(f"\n--- {fname}: {len(df)} строк ---")

        for idx, row in df.iterrows():
            try:
                deal_id = extract_deal_id(row.get("Номер телефона"))
                operator_name = re.sub(r"\s*\(.*?\)\s*", "", str(row.get("Оператор", ""))).strip()
                department = str(row.get("Отдел", "")).strip()
                eval_date = parse_date(row.get("Дата коммуникации"))
                rated_date = parse_date(row.get("Дата оценки"))
                general_comment = clean_comment(row.get("Общий комментарий"))

                if not operator_name:
                    print(f"  Строка {idx}: нет имени оператора, пропуск")
                    skipped += 1
                    continue

                # Проверка дубля: deal_id + checklist_id
                if deal_id:
                    existing = (
                        db.query(Evaluation)
                        .filter(
                            Evaluation.deal_id == deal_id,
                            Evaluation.checklist_id == CHECKLIST_ID,
                        )
                        .first()
                    )
                    if existing:
                        print(f"  Строка {idx}: сделка {deal_id} уже есть (ev.id={existing.id}), пропуск")
                        skipped += 1
                        continue

                # Вычисляем week/month из даты звонка
                week_num = eval_date.isocalendar()[1] if eval_date else None
                week_year = eval_date.isocalendar()[0] if eval_date else None
                month_name = MONTH_NAMES[eval_date.month - 1] if eval_date else None

                ev = Evaluation(
                    checklist_id=CHECKLIST_ID,
                    deal_id=deal_id,
                    deal_url=(
                        f"https://entera.bitrix24.ru/crm/deal/details/{deal_id}/"
                        if deal_id else None
                    ),
                    operator_name=operator_name,
                    department=department,
                    eval_date=eval_date,
                    week_num=week_num,
                    week_year=week_year,
                    month=month_name,
                    evaluator_id=EVALUATOR_USER_ID,
                    general_comment=general_comment,
                    status="published",
                    created_at=rated_date or datetime.now(),
                    updated_at=rated_date,
                )
                db.add(ev)
                db.flush()  # получаем ev.id

                # Создаём EvaluationItem по каждому критерию
                items_to_score = []
                for col, crit_id in COLUMN_TO_CRIT.items():
                    value = to_value(row.get(col))
                    comment_col = f"Комментарии - {col}"
                    comment = clean_comment(row.get(comment_col))
                    item = EvaluationItem(
                        evaluation_id=ev.id,
                        criterion_id=crit_id,
                        value=value,
                        comment=comment,
                    )
                    db.add(item)
                    items_to_score.append(item)

                # Подсчёт итогового балла
                total_score, _ = calculate_scores(items_to_score, checklist)
                ev.total_score = total_score

                # Категория клиента из Битрикс24
                if deal_id:
                    try:
                        cached = db.query(DealCache).filter(DealCache.deal_id == deal_id).first()
                        if cached and cached.client_category:
                            ev.client_category = cached.client_category
                        else:
                            info = get_deal(deal_id)
                            if info and info.client_category:
                                ev.client_category = info.client_category
                                if cached:
                                    cached.client_category = info.client_category
                    except Exception:
                        pass

                imported += 1
                print(f"  Строка {idx}: {operator_name} | сделка {deal_id} | {eval_date} | итог {total_score}%")

            except Exception as e:
                errors.append((fname, idx, str(e)))
                print(f"  Строка {idx}: ОШИБКА — {e}")

    db.commit()
    db.close()

    print(f"\n=== ИТОГ ===")
    print(f"Импортировано: {imported}")
    print(f"Пропущено (дубли / без имени): {skipped}")
    if errors:
        print(f"Ошибки ({len(errors)}):")
        for e in errors:
            print(f"  {e}")


if __name__ == "__main__":
    main()
