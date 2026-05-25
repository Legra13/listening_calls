"""
Скрипт для пакетного дозаполнения client_category в evaluations и deal_cache.

Делает один SQL-запрос к Битрикс MySQL для всех уникальных deal_id,
затем обновляет SQLite.

Запуск:
  cd /home/egerasimchuk/Инструмент_прослушки_звонков
  python3 -m scripts.backfill_categories
"""
from __future__ import annotations
import sys
import os

# Добавляем корень проекта в PYTHONPATH
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sqlite3
import pymysql
import pymysql.cursors
from datetime import datetime
from urllib.parse import urlparse

from app.config import BITRIX_MYSQL_URL

_DEALS_TABLE       = "b24-entera-bitrix24-ru-deals"
_ENUMERATIONS_TABLE = "b24-entera-bitrix24-ru-enumerations"
_USERS_TABLE       = "b24-entera-bitrix24-ru-users"
_DEPTS_TABLE       = "b24-entera-bitrix24-ru-departments"
_CATEGORY_FIELD    = "UF_CRM_1690299302751"

SQLITE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "app.db")


def get_bitrix_conn():
    parsed = urlparse(BITRIX_MYSQL_URL)
    return pymysql.connect(
        host=parsed.hostname,
        port=parsed.port or 3306,
        user=parsed.username,
        password=parsed.password,
        database=parsed.path.lstrip("/"),
        charset="utf8mb4",
        connect_timeout=15,
        cursorclass=pymysql.cursors.DictCursor,
    )


def main():
    sqlite_conn = sqlite3.connect(SQLITE_PATH)
    sqlite_conn.row_factory = sqlite3.Row
    cur = sqlite_conn.cursor()

    # 1. Собираем уникальные числовые deal_id без категории
    cur.execute("""
        SELECT DISTINCT deal_id
        FROM evaluations
        WHERE deal_id IS NOT NULL
          AND deal_id != ''
          AND LENGTH(deal_id) <= 8
          AND CAST(deal_id AS INTEGER) > 0
          AND (client_category IS NULL OR client_category = '')
    """)
    deal_ids = [row[0] for row in cur.fetchall()]
    print(f"[INFO] Нужно обновить категории для {len(deal_ids)} уникальных сделок")

    if not deal_ids:
        print("[OK] Нечего обновлять.")
        return

    # 2. Пакетный запрос к Битрикс — все нужные сделки за раз
    print("[INFO] Подключаемся к Битрикс MySQL...")
    bx_conn = get_bitrix_conn()

    int_ids = [int(d) for d in deal_ids]
    placeholders = ",".join(["%s"] * len(int_ids))

    # Загружаем возможные значения категории (A/B/C/D) напрямую из сделок
    # Сначала берём все уникальные cat_id, которые используются в нужных сделках
    with bx_conn.cursor() as cur_bx:
        cur_bx.execute(
            f"SELECT DISTINCT `{_CATEGORY_FIELD}` FROM `{_DEALS_TABLE}` "
            f"WHERE ID IN ({placeholders}) AND `{_CATEGORY_FIELD}` IS NOT NULL",
            int_ids,
        )
        cat_id_rows = cur_bx.fetchall()
    cat_ids = [r[_CATEGORY_FIELD] for r in cat_id_rows]

    enum_map: dict[int, str] = {}
    if cat_ids:
        placeholders_enum = ",".join(["%s"] * len(cat_ids))
        with bx_conn.cursor() as cur_bx:
            cur_bx.execute(
                f"SELECT ID, VALUE FROM `{_ENUMERATIONS_TABLE}` WHERE ID IN ({placeholders_enum})",
                cat_ids,
            )
            for row in cur_bx.fetchall():
                enum_map[row["ID"]] = row["VALUE"]
    print(f"[INFO] Загружены значения enum категории: {enum_map}")

    with bx_conn.cursor() as cur_bx:
        cur_bx.execute(
            f"""
            SELECT d.ID,
                   d.{_CATEGORY_FIELD} AS cat_id
            FROM `{_DEALS_TABLE}` d
            WHERE d.ID IN ({placeholders})
            """,
            int_ids,
        )
        bx_rows = cur_bx.fetchall()

    bx_conn.close()

    # Строим маппинг deal_id → category_value
    deal_category: dict[str, str] = {}
    for row in bx_rows:
        cat_id = row.get("cat_id")
        if cat_id and cat_id in enum_map:
            deal_category[str(row["ID"])] = enum_map[cat_id]
        else:
            deal_category[str(row["ID"])] = ""  # нет категории в Битрикс

    found = sum(1 for v in deal_category.values() if v)
    print(f"[INFO] Из Битрикс: найдено {found} сделок с категорией, {len(deal_category)-found} без категории")

    # 3. Обновляем evaluations
    updated_evals = 0
    for deal_id, category in deal_category.items():
        if not category:
            continue
        cur.execute(
            "UPDATE evaluations SET client_category = ? WHERE deal_id = ? AND (client_category IS NULL OR client_category = '')",
            (category, deal_id),
        )
        updated_evals += cur.rowcount

    # 4. Обновляем deal_cache
    updated_cache = 0
    for deal_id, category in deal_category.items():
        if not category:
            continue
        cur.execute(
            "UPDATE deal_cache SET client_category = ? WHERE deal_id = ? AND (client_category IS NULL OR client_category = '')",
            (category, deal_id),
        )
        updated_cache += cur.rowcount

    sqlite_conn.commit()
    sqlite_conn.close()

    print(f"[OK] Обновлено evaluations: {updated_evals}")
    print(f"[OK] Обновлено deal_cache: {updated_cache}")
    print("[DONE]")


if __name__ == "__main__":
    main()
