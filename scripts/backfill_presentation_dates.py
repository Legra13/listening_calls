"""
Пересчёт presentation_date в deal_cache по новой логике выбора поля даты
презентации в зависимости от отдела (продление vs остальные).

Причина: раньше дата презентации бралась только из поля продления
(UF_CRM_1654694803). Для ОП и прочих отделов нужно общее поле
"Дата презентации" (UF_CRM_1560328872). Скрипт дозаполняет/исправляет кеш.

Запуск (из корня проекта):
    python3 scripts/backfill_presentation_dates.py
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import SessionLocal
from app.models import DealCache
from app.bitrix import (
    _get_connection,
    _to_date,
    _renewal_dept_ids,
    _PRESENTATION_FIELD_RENEWAL,
    _PRESENTATION_FIELD_GENERAL,
    _DEALS_TABLE,
    _DEPTS_TABLE,
)


def main() -> None:
    db = SessionLocal()
    try:
        entries = db.query(DealCache).all()
        print(f"Кешировано сделок: {len(entries)}")

        # Батч-запрос обоих полей презентации по всем deal_id
        deal_ids = [e.deal_id for e in entries if e.deal_id]
        pres_map: dict[str, tuple] = {}
        conn = _get_connection()
        try:
            # Названия отделов продления из дерева оргструктуры (в кеше хранится имя отдела)
            renewal_ids = _renewal_dept_ids(conn)
            with conn.cursor() as cur:
                placeholders = ",".join(["%s"] * len(renewal_ids)) or "NULL"
                cur.execute(
                    f"SELECT NAME FROM `{_DEPTS_TABLE}` WHERE ID IN ({placeholders})",
                    list(renewal_ids),
                )
                renewal_names = {r["NAME"] for r in cur.fetchall() if r.get("NAME")}

            CHUNK = 500
            for i in range(0, len(deal_ids), CHUNK):
                chunk = deal_ids[i : i + CHUNK]
                placeholders = ",".join(["%s"] * len(chunk))
                with conn.cursor() as cur:
                    cur.execute(
                        f"SELECT ID, {_PRESENTATION_FIELD_RENEWAL}, {_PRESENTATION_FIELD_GENERAL} "
                        f"FROM `{_DEALS_TABLE}` WHERE ID IN ({placeholders})",
                        [int(x) for x in chunk],
                    )
                    for row in cur.fetchall():
                        pres_map[str(row["ID"])] = (
                            _to_date(row.get(_PRESENTATION_FIELD_RENEWAL)),
                            _to_date(row.get(_PRESENTATION_FIELD_GENERAL)),
                        )
        finally:
            conn.close()

        changed = 0
        for e in entries:
            pair = pres_map.get(e.deal_id)
            if pair is None:
                continue  # сделки нет в Битрикс
            pres_renewal, pres_general = pair
            if e.department in renewal_names:
                new_val = pres_renewal or pres_general
            else:
                new_val = pres_general or pres_renewal

            old_val = e.presentation_date.date() if e.presentation_date else None
            if new_val != old_val:
                e.presentation_date = new_val
                changed += 1

        db.commit()
        print(f"Обновлено записей: {changed}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
