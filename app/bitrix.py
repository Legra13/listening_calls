"""
Интеграция с Битрикс24 MySQL.
Подключается напрямую к реплике MySQL.
"""
from __future__ import annotations
from datetime import datetime, date
from dataclasses import dataclass, field
import json
import pymysql
import pymysql.cursors
from app.config import BITRIX_MYSQL_URL

# stage semantic_id → человеко-читаемое название (логика из logic_summary.md)
SEMANTIC_TO_STAGE: dict[str, str] = {
    "S": "сделка успешна",
    "F": "не смог продать",
    "P": "в работе",
}

_DEALS_TABLE  = "b24-entera-bitrix24-ru-deals"
_USERS_TABLE  = "b24-entera-bitrix24-ru-users"
_DEPTS_TABLE  = "b24-entera-bitrix24-ru-departments"


_CATEGORY_FIELD = "UF_CRM_1690299302751"
_ENUMERATIONS_TABLE = "b24-entera-bitrix24-ru-enumerations"


@dataclass
class DealInfo:
    deal_id: str
    operator_name: str
    deal_date: datetime | None
    stage: str          # "сделка успешна" | "не смог продать" | "в работе"
    title: str | None
    department: str | None = None
    presentation_date: date | None = None   # UF_CRM_1654694803 — Продление дата "Провели презентацию"
    client_category: str | None = None      # UF_CRM_1690299302751 — Категория клиента (A/B/C/D)
    close_date: date | None = None          # CLOSEDATE — дата закрытия сделки


def _get_connection() -> pymysql.Connection:
    """Парсим BITRIX_MYSQL_URL и создаём соединение."""
    if not BITRIX_MYSQL_URL:
        raise ConnectionError("BITRIX_MYSQL_URL не задан в .env")

    # mysql+pymysql://user:pass@host:port/db
    from urllib.parse import urlparse
    parsed = urlparse(BITRIX_MYSQL_URL)
    return pymysql.connect(
        host=parsed.hostname,
        port=parsed.port or 3306,
        user=parsed.username,
        password=parsed.password,
        database=parsed.path.lstrip("/"),
        charset="utf8mb4",
        connect_timeout=8,
        cursorclass=pymysql.cursors.DictCursor,
    )


def get_deal(deal_id: str | int) -> DealInfo | None:
    """
    Получает данные сделки по ID из Битрикс MySQL.
    Возвращает None если сделка не найдена.
    """
    try:
        conn = _get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT d.ID, d.TITLE, d.STAGE_SEMANTIC_ID,
                           d.ASSIGNED_BY_ID, d.DATE_CREATE, d.CLOSEDATE,
                           d.UF_CRM_1654694803,
                           d.{_CATEGORY_FIELD},
                           u.NAME, u.LAST_NAME, u.UF_DEPARTMENT
                    FROM `{_DEALS_TABLE}` d
                    LEFT JOIN `{_USERS_TABLE}` u ON u.ID = d.ASSIGNED_BY_ID
                    WHERE d.ID = %s
                    LIMIT 1
                    """,
                    (int(deal_id),),
                )
                row = cur.fetchone()

            if not row:
                return None

            name_parts = [row.get("NAME") or "", row.get("LAST_NAME") or ""]
            operator_name = " ".join(p for p in name_parts if p).strip() or f"Пользователь #{row['ASSIGNED_BY_ID']}"

            semantic = row.get("STAGE_SEMANTIC_ID") or "P"
            stage = SEMANTIC_TO_STAGE.get(semantic, "в работе")

            # Определяем отдел: UF_DEPARTMENT — JSON-массив ID, берём первый
            department: str | None = None
            try:
                dept_ids = json.loads(row.get("UF_DEPARTMENT") or "[]")
            except (ValueError, TypeError):
                dept_ids = []
            if dept_ids:
                with conn.cursor() as cur2:
                    cur2.execute(
                        f"SELECT NAME FROM `{_DEPTS_TABLE}` WHERE ID = %s LIMIT 1",
                        (dept_ids[0],),
                    )
                    dept_row = cur2.fetchone()
                    if dept_row:
                        department = dept_row["NAME"]

            raw_pres = row.get("UF_CRM_1654694803")
            presentation_date: date | None = None
            if isinstance(raw_pres, date):
                presentation_date = raw_pres
            elif isinstance(raw_pres, datetime):
                presentation_date = raw_pres.date()

            raw_close = row.get("CLOSEDATE")
            close_date: date | None = None
            if isinstance(raw_close, date):
                close_date = raw_close
            elif isinstance(raw_close, datetime):
                close_date = raw_close.date()

            # Категория клиента — int → расшифровка через enumerations
            client_category: str | None = None
            cat_id = row.get(_CATEGORY_FIELD)
            if cat_id:
                with conn.cursor() as cur3:
                    cur3.execute(
                        f"SELECT VALUE FROM `{_ENUMERATIONS_TABLE}` WHERE ID = %s LIMIT 1",
                        (cat_id,),
                    )
                    cat_row = cur3.fetchone()
                    if cat_row:
                        client_category = cat_row["VALUE"]

            return DealInfo(
                deal_id=str(row["ID"]),
                operator_name=operator_name,
                deal_date=row.get("DATE_CREATE"),
                stage=stage,
                title=row.get("TITLE"),
                department=department,
                presentation_date=presentation_date,
                client_category=client_category,
                close_date=close_date,
            )
        finally:
            conn.close()

    except ConnectionError:
        raise
    except Exception as exc:
        raise ConnectionError(f"Ошибка подключения к Битрикс: {exc}") from exc


def get_departments() -> list[dict]:
    """Возвращает список отделов из Битрикс24."""
    try:
        conn = _get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT ID, NAME FROM `{_DEPTS_TABLE}` ORDER BY NAME")
                rows = cur.fetchall()
            return [{"id": r["ID"], "name": r["NAME"]} for r in rows if r.get("NAME")]
        finally:
            conn.close()
    except Exception:
        return []


def get_employees() -> list[dict]:
    """Возвращает список активных сотрудников из Битрикс24."""
    try:
        conn = _get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT ID, NAME, LAST_NAME, UF_DEPARTMENT FROM `{_USERS_TABLE}` "
                    "WHERE ACTIVE = 1 ORDER BY LAST_NAME, NAME",
                )
                rows = cur.fetchall()

            dept_cache: dict[int, str] = {}
            result = []
            for row in rows:
                parts = [row.get("LAST_NAME") or "", row.get("NAME") or ""]
                full_name = " ".join(p for p in parts if p).strip()
                if not full_name:
                    continue
                dept_ids = []
                try:
                    dept_ids = json.loads(row.get("UF_DEPARTMENT") or "[]")
                except Exception:
                    pass
                dept_name = ""
                if dept_ids:
                    did = dept_ids[0]
                    if did not in dept_cache:
                        with conn.cursor() as cur2:
                            cur2.execute(
                                f"SELECT NAME FROM `{_DEPTS_TABLE}` WHERE ID = %s LIMIT 1",
                                (did,),
                            )
                            dr = cur2.fetchone()
                            dept_cache[did] = dr["NAME"] if dr else ""
                    dept_name = dept_cache.get(did, "")
                result.append({"name": full_name, "department": dept_name})
            return result
        finally:
            conn.close()
    except Exception:
        return []
