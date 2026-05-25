"""
Аналитика звонков: агрегация данных для отчётов.
Логика по logic_summary.md §§3–7.
"""
from __future__ import annotations
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
import json

from sqlalchemy.orm import Session, selectinload

from app.models import Block, Checklist, Criterion, Evaluation, EvaluationItem, User
from app.scoring import calculate_scores

WON = "сделка успешна"


def _comment_to_text(raw: str | None) -> str | None:
    """Извлекает plain-text из JSON-комментария ([{text, flag, time}, ...])."""
    if not raw:
        return None
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            texts = [c["text"] for c in data if isinstance(c, dict) and c.get("text")]
            return "; ".join(texts) if texts else None
    except (json.JSONDecodeError, ValueError):
        pass
    return raw or None
LOST = "не смог продать"


# ── Цвет тепловой карты (logic_summary §5) ────────────────────────────────────

def heat_bg(v: float | None) -> str:
    if v is None:
        return ""
    v = max(0.0, min(100.0, v))
    if v <= 40:
        t = v / 40
        r = round(239 + (251 - 239) * t)
        g = round(68 + (146 - 68) * t)
        b = round(68 + (60 - 68) * t)
    elif v <= 60:
        t = (v - 40) / 20
        r = round(251 + (250 - 251) * t)
        g = round(146 + (204 - 146) * t)
        b = round(60 + (21 - 60) * t)
    else:
        t = (v - 60) / 40
        r = round(250 + (34 - 250) * t)
        g = round(204 + (197 - 204) * t)
        b = round(21 + (94 - 21) * t)
    return f"rgb({r},{g},{b})"


def heat_style(v: float | None) -> str:
    if v is None:
        return "color:#94a3b8"
    return f"background:{heat_bg(v)};color:#1e293b"


def delta_style(d: float | None) -> str:
    if d is None:
        return "color:#94a3b8"
    return "color:#16a34a;font-weight:600" if d >= 0 else "color:#dc2626;font-weight:600"


# ── Фильтры ───────────────────────────────────────────────────────────────────

@dataclass
class Filters:
    operators: list[str] = field(default_factory=list)
    departments: list[str] = field(default_factory=list)
    date_from: date | None = None
    date_to: date | None = None
    checklist_id: int | None = None
    stage: str = ""   # "" = все; "won" | "lost" | "progress"


# ── Загрузка ─────────────────────────────────────────────────────────────────

def fetch_evaluations(db: Session, filters: Filters) -> list[Evaluation]:
    q = (
        db.query(Evaluation)
        .options(
            selectinload(Evaluation.items).selectinload(EvaluationItem.criterion),
            selectinload(Evaluation.checklist)
              .selectinload(Checklist.blocks)
              .selectinload(Block.criteria),
        )
    )
    if filters.departments:
        q = q.filter(Evaluation.department.in_(filters.departments))
    if filters.operators:
        q = q.filter(Evaluation.operator_name.in_(filters.operators))
    if filters.date_from:
        q = q.filter(Evaluation.eval_date >= datetime.combine(filters.date_from, datetime.min.time()))
    if filters.date_to:
        q = q.filter(Evaluation.eval_date <= datetime.combine(filters.date_to, datetime.max.time()))
    if filters.checklist_id:
        q = q.filter(Evaluation.checklist_id == filters.checklist_id)
    _STAGE_MAP = {
        "won":      "сделка успешна",
        "lost":     "не смог продать",
        "progress": "в работе",
    }
    if filters.stage and filters.stage in _STAGE_MAP:
        q = q.filter(Evaluation.stage == _STAGE_MAP[filters.stage])
    q = q.filter(Evaluation.status == "published")
    return q.all()


def get_filter_options(db: Session) -> dict:
    from sqlalchemy import func as sa_func
    op_rows = (
        db.query(Evaluation.operator_name, Evaluation.department, sa_func.count(Evaluation.id))
        .filter(Evaluation.operator_name.isnot(None), Evaluation.operator_name != "")
        .group_by(Evaluation.operator_name, Evaluation.department)
        .order_by(Evaluation.operator_name)
        .all()
    )
    # merge rows with same operator name (different depts edge case), sum counts
    seen: dict[str, dict] = {}
    for name, dept, cnt in op_rows:
        if name not in seen:
            seen[name] = {"name": name, "dept": dept or "", "count": cnt}
        else:
            seen[name]["count"] += cnt
    operators = list(seen.values())

    checklists = db.query(Checklist).filter(Checklist.status == "active").all()
    # Отделы с привязкой к активным чек-листам:
    # — явно заданные в настройках чек-листа
    # — или взятые из оценок, если привязка не настроена
    dept_set: set[str] = set()
    for cl in checklists:
        if cl.departments:
            for d in cl.departments.split(","):
                d = d.strip()
                if d:
                    dept_set.add(d)
        else:
            rows = (
                db.query(Evaluation.department)
                .filter(
                    Evaluation.checklist_id == cl.id,
                    Evaluation.department.isnot(None),
                    Evaluation.department != "",
                    Evaluation.status == "published",
                )
                .distinct()
                .all()
            )
            for (d,) in rows:
                dept_set.add(d)
    departments = sorted(dept_set)
    return {
        "operators": operators,
        "departments": departments,
        "checklists": checklists,
    }


# ── Вспомогательные ────────────────────────────────────────────────────────────

def _avg(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 1) if values else None


def _calc_total(block_scores: dict[int, float | None], blocks: list) -> float | None:
    """Взвешенный итог по блокам — блоки с None (все NA) исключаются."""
    w_num = w_den = 0.0
    for bl in blocks:
        s = block_scores.get(bl.id)
        if s is not None:
            w_num += s * bl.weight
            w_den += bl.weight
    return round(w_num / w_den, 1) if w_den > 0 else None


def _wr(rows: list[dict]) -> float | None:
    if not rows:
        return None
    won = sum(1 for r in rows if r["ev"].stage == WON)
    return round(won / len(rows) * 100, 1)


def prep_rows(evaluations: list[Evaluation]) -> list[dict]:
    result = []
    for ev in evaluations:
        if ev.checklist is None:
            continue
        _, block_scores = calculate_scores(ev.items, ev.checklist)
        result.append({"ev": ev, "block_scores": block_scores, "close_date": None})
    return result


def attach_close_dates(rows: list[dict], db: Session) -> None:
    """Подтягивает close_date из DealCache и дописывает в каждый row."""
    from app.models import DealCache
    deal_ids = list({r["ev"].deal_id for r in rows if r["ev"].deal_id})
    if not deal_ids:
        return
    cache_rows = db.query(DealCache).filter(DealCache.deal_id.in_(deal_ids)).all()
    close_map: dict[str, date | None] = {c.deal_id: (c.close_date.date() if isinstance(c.close_date, datetime) else c.close_date) for c in cache_rows}
    for r in rows:
        r["close_date"] = close_map.get(r["ev"].deal_id)


# ── KPI ──────────────────────────────────────────────────────────────────────

def compute_kpi(rows: list[dict]) -> dict:
    if not rows:
        return {"count": 0, "avg_score": None, "won": 0, "lost": 0, "won_pct": None, "lost_pct": None}
    count = len(rows)
    scores = [float(r["ev"].total_score) for r in rows if r["ev"].total_score is not None]
    avg_score = _avg(scores)
    won = sum(1 for r in rows if r["ev"].stage == WON)
    lost = sum(1 for r in rows if r["ev"].stage == LOST)
    closed = won + lost
    won_pct = round(won / closed * 100, 1) if closed else None
    lost_pct = round(lost / closed * 100, 1) if closed else None
    return {"count": count, "avg_score": avg_score, "won": won, "lost": lost,
            "won_pct": won_pct, "lost_pct": lost_pct,
            "win_rate": won_pct}   # алиас: won / (won+lost) * 100


# ── Tab 1 — Общие показатели ─────────────────────────────────────────────────

def compute_tab1(rows: list[dict], checklist: Checklist) -> dict:
    blocks = list(checklist.blocks)
    operators = sorted({r["ev"].operator_name for r in rows})

    hm_rows = []
    for op in operators:
        op_rows = [r for r in rows if r["ev"].operator_name == op]
        cells = []
        for block in blocks:
            vals = [r["block_scores"][block.id] for r in op_rows
                    if r["block_scores"].get(block.id) is not None]
            pct = _avg(vals)
            pts = round(pct / 100 * block.weight, 1) if pct is not None else None
            cells.append({"pct": pct, "pts": pts})
        total = _avg([_calc_total(r["block_scores"], blocks) for r in op_rows])
        won = sum(1 for r in op_rows if r["ev"].stage == WON)
        lost = sum(1 for r in op_rows if r["ev"].stage == LOST)
        closed = won + lost
        hm_rows.append({
            "name": op,
            "cells": cells,
            "total": total,
            "count": len(op_rows),
            "won": won,
            "lost": lost,
            "won_pct": round(won / closed * 100, 1) if closed else None,
            "lost_pct": round(lost / closed * 100, 1) if closed else None,
        })

    team_cells = []
    for block in blocks:
        vals = [r["block_scores"][block.id] for r in rows
                if r["block_scores"].get(block.id) is not None]
        pct = _avg(vals)
        pts = round(pct / 100 * block.weight, 1) if pct is not None else None
        team_cells.append({"pct": pct, "pts": pts})
    team_total = _avg([_calc_total(r["block_scores"], blocks) for r in rows])
    team_won = sum(1 for r in rows if r["ev"].stage == WON)
    team_lost = sum(1 for r in rows if r["ev"].stage == LOST)
    team_closed = team_won + team_lost

    weeks: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        ev = r["ev"]
        if ev.week_year and ev.week_num and ev.total_score is not None:
            key = f"{ev.week_year}-W{ev.week_num:02d}"
            weeks[key].append(float(ev.total_score))
    weekly = [
        {"week": k, "count": len(v), "avg": _avg(v)}
        for k, v in sorted(weeks.items())
    ]

    return {
        "blocks": blocks,
        "hm_rows": hm_rows,
        "team_cells": team_cells,
        "team_total": team_total,
        "team_won_pct": round(team_won / team_closed * 100, 1) if team_closed else None,
        "team_lost_pct": round(team_lost / team_closed * 100, 1) if team_closed else None,
        "weekly": weekly,
    }


# ── Tab 2 — Корреляция по блокам ─────────────────────────────────────────────

def compute_tab2(rows: list[dict], checklist: Checklist) -> list[dict]:
    blocks = list(checklist.blocks)
    won_rows = [r for r in rows if r["ev"].stage == WON]
    lost_rows = [r for r in rows if r["ev"].stage == LOST]

    result = []
    for block in blocks:
        bid = block.id
        # Исключаем строки, где блок не применялся (None = все NA)
        won_vals = [r["block_scores"][bid] for r in won_rows if r["block_scores"].get(bid) is not None]
        lost_vals = [r["block_scores"][bid] for r in lost_rows if r["block_scores"].get(bid) is not None]
        avg_won = _avg(won_vals) if won_vals else None
        avg_lost = _avg(lost_vals) if lost_vals else None
        delta = round(avg_won - avg_lost, 1) if (avg_won is not None and avg_lost is not None) else None

        applicable = [r for r in rows if r["block_scores"].get(bid) is not None]
        done = [r for r in applicable if r["block_scores"][bid] > 0]
        not_done = [r for r in applicable if r["block_scores"][bid] == 0.0]
        wr_done = _wr(done)
        wr_not_done = _wr(not_done)
        wr_impact = round(wr_done - wr_not_done, 1) if (wr_done is not None and wr_not_done is not None) else None

        result.append({
            "name": block.display_name or block.name,
            "weight": block.weight,
            "avg_won": avg_won,
            "avg_lost": avg_lost,
            "delta": delta,
            "wr_done": wr_done,
            "wr_not_done": wr_not_done,
            "wr_impact": wr_impact,
        })

    result.sort(key=lambda x: (x["delta"] is None, -(x["delta"] or 0)))
    return result


# ── Tab 3 — Корреляция по сотрудникам ────────────────────────────────────────

def compute_tab3(rows: list[dict], checklist: Checklist) -> dict:
    blocks = list(checklist.blocks)
    operators = sorted({r["ev"].operator_name for r in rows})

    def _t1_cells(subset: list[dict]) -> list[dict]:
        won_r = [r for r in subset if r["ev"].stage == WON]
        lost_r = [r for r in subset if r["ev"].stage == LOST]
        cells = []
        for block in blocks:
            bid = block.id
            won_vals = [r["block_scores"][bid] for r in won_r if r["block_scores"].get(bid) is not None]
            lost_vals = [r["block_scores"][bid] for r in lost_r if r["block_scores"].get(bid) is not None]
            avg_won = _avg(won_vals) if won_vals else None
            avg_lost = _avg(lost_vals) if lost_vals else None
            delta = round(avg_won - avg_lost, 1) if (avg_won is not None and avg_lost is not None) else None
            cells.append({"won": avg_won, "lost": avg_lost, "delta": delta})
        return cells

    t1_rows = [
        {"name": op, "cells": _t1_cells([r for r in rows if r["ev"].operator_name == op])}
        for op in operators
    ]
    team_cells = _t1_cells(rows)

    RANGES = [
        ("0%",      lambda v: v is not None and v == 0.0),
        ("1–40%",   lambda v: v is not None and 0 < v <= 40),
        ("40–70%",  lambda v: v is not None and 40 < v <= 70),
        ("70–100%", lambda v: v is not None and 70 < v <= 100),
    ]
    t2_rows = []
    for label, range_fn in RANGES:
        cells = []
        for block in blocks:
            bid = block.id
            subset = [r for r in rows if range_fn(r["block_scores"].get(bid))]
            if not subset:
                cells.append({"wr": None, "n": 0})
                continue
            closed = [r for r in subset if r["ev"].stage in (WON, LOST)]
            won_c = sum(1 for r in closed if r["ev"].stage == WON)
            wr = round(won_c / len(closed) * 100, 1) if closed else None
            cells.append({"wr": wr, "n": len(subset)})
        t2_rows.append({"label": label, "cells": cells})

    return {
        "blocks": blocks,
        "t1_rows": t1_rows,
        "team_cells": team_cells,
        "t2_rows": t2_rows,
    }


# ── Отчёт «Результаты сотрудников» ───────────────────────────────────────────

@dataclass
class EmployeeFilters:
    operators: list[str] = field(default_factory=list)
    departments: list[str] = field(default_factory=list)
    call_date_from: date | None = None
    call_date_to: date | None = None
    rated_date_from: date | None = None
    rated_date_to: date | None = None
    checklist_id: int | None = None
    evaluator_id: int | None = None
    stage: str | None = None
    client_category: list[str] = field(default_factory=list)
    display_mode: str = "pct"    # "pct" | "pts"
    group_mode: str = "groups"   # "groups" | "criteria"
    show_comments: bool = True


def get_employee_report_options(db: Session) -> dict:
    from sqlalchemy import func as sa_func

    op_rows = (
        db.query(Evaluation.operator_name, Evaluation.department, sa_func.count(Evaluation.id))
        .filter(Evaluation.operator_name.isnot(None), Evaluation.operator_name != "")
        .filter(Evaluation.status == "published")
        .group_by(Evaluation.operator_name, Evaluation.department)
        .order_by(Evaluation.operator_name)
        .all()
    )
    seen: dict[str, dict] = {}
    for name, dept, cnt in op_rows:
        if name not in seen:
            seen[name] = {"name": name, "dept": dept or "", "count": cnt}
        else:
            seen[name]["count"] += cnt
    operators = list(seen.values())

    dept_rows = (
        db.query(Evaluation.department)
        .filter(Evaluation.department.isnot(None), Evaluation.department != "")
        .distinct()
        .order_by(Evaluation.department)
        .all()
    )

    checklists = db.query(Checklist).filter(Checklist.status == "active").all()

    evaluator_ids_sub = (
        db.query(Evaluation.evaluator_id)
        .filter(Evaluation.evaluator_id.isnot(None), Evaluation.status == "published")
        .distinct()
        .subquery()
    )
    evaluators = (
        db.query(User)
        .filter(User.id.in_(evaluator_ids_sub))
        .order_by(User.full_name)
        .all()
    )

    stage_rows = (
        db.query(Evaluation.stage)
        .filter(Evaluation.stage.isnot(None), Evaluation.stage != "")
        .distinct()
        .order_by(Evaluation.stage)
        .all()
    )

    return {
        "operators": operators,
        "departments": [r[0] for r in dept_rows],
        "checklists": checklists,
        "evaluators": evaluators,
        "stages": [r[0] for r in stage_rows],
    }


def fetch_evaluations_employee(db: Session, filters: EmployeeFilters) -> list[Evaluation]:
    q = (
        db.query(Evaluation)
        .options(
            selectinload(Evaluation.items).selectinload(EvaluationItem.criterion),
            selectinload(Evaluation.checklist)
              .selectinload(Checklist.blocks)
              .selectinload(Block.criteria),
            selectinload(Evaluation.evaluator),
        )
    )
    if filters.departments:
        q = q.filter(Evaluation.department.in_(filters.departments))
    if filters.operators:
        q = q.filter(Evaluation.operator_name.in_(filters.operators))
    if filters.call_date_from:
        q = q.filter(Evaluation.eval_date >= datetime.combine(filters.call_date_from, datetime.min.time()))
    if filters.call_date_to:
        q = q.filter(Evaluation.eval_date <= datetime.combine(filters.call_date_to, datetime.max.time()))
    if filters.rated_date_from:
        q = q.filter(Evaluation.created_at >= datetime.combine(filters.rated_date_from, datetime.min.time()))
    if filters.rated_date_to:
        q = q.filter(Evaluation.created_at <= datetime.combine(filters.rated_date_to, datetime.max.time()))
    if filters.checklist_id:
        q = q.filter(Evaluation.checklist_id == filters.checklist_id)
    if filters.evaluator_id:
        q = q.filter(Evaluation.evaluator_id == filters.evaluator_id)
    if filters.stage:
        q = q.filter(Evaluation.stage == filters.stage)
    if filters.client_category:
        q = q.filter(Evaluation.client_category.in_(filters.client_category))
    q = q.filter(Evaluation.status == "published")
    q = q.order_by(Evaluation.eval_date.desc().nullslast(), Evaluation.created_at.desc())
    return q.all()


def compute_employee_report(evaluations: list[Evaluation], checklist: Checklist, group_mode: str = "groups") -> dict:
    """Данные для отчёта «Результаты сотрудников по чек-листу»."""
    blocks = list(checklist.blocks)

    all_ci = []  # {"block": ..., "crit": ...}
    for block in blocks:
        for crit in block.criteria:
            all_ci.append({"block": block, "crit": crit})

    # Определяем структуру столбцов
    if group_mode == "criteria":
        columns = [
            {
                "id": ci["crit"].id,
                "label": ci["crit"].text,
                "label_short": (ci["crit"].text[:22] + "…") if len(ci["crit"].text) > 22 else ci["crit"].text,
                "weight": ci["crit"].weight,
            }
            for ci in all_ci
        ]
        column_groups = []
        for block in blocks:
            cnt = sum(1 for ci in all_ci if ci["block"].id == block.id)
            if cnt:
                column_groups.append({
                    "name": block.display_name or block.name,
                    "weight": block.weight,
                    "count": cnt,
                })
    else:
        columns = [
            {
                "id": block.id,
                "label": block.display_name or block.name,
                "label_short": (
                    (block.display_name or block.name)[:18] + "…"
                    if len(block.display_name or block.name) > 18
                    else (block.display_name or block.name)
                ),
                "weight": block.weight,
            }
            for block in blocks
        ]
        column_groups = None

    # Детальные строки (одна строка = одна оценка)
    detail_rows = []
    for ev in evaluations:
        _, block_scores = calculate_scores(ev.items, checklist)
        total = _calc_total(block_scores, blocks)

        crit_values: dict[int, str] = {}
        crit_comments: dict[int, str] = {}
        for item in ev.items:
            crit_values[item.criterion_id] = item.value
            text = _comment_to_text(item.comment)
            if text:
                crit_comments[item.criterion_id] = text

        if group_mode == "criteria":
            cells = [
                {"value": crit_values.get(ci["crit"].id), "comment": crit_comments.get(ci["crit"].id)}
                for ci in all_ci
            ]
        else:
            cells = []
            for block in blocks:
                pct = block_scores.get(block.id)
                pts = round(pct / 100 * block.weight, 1) if pct is not None else None
                block_crits = sorted(block.criteria, key=lambda x: x.order_index)
                comments = [crit_comments[c.id] for c in block_crits if c.id in crit_comments]
                cells.append({"pct": pct, "pts": pts, "comments": comments})

        detail_rows.append({
            "ev": ev,
            "cells": cells,
            "total": total,
        })

    # Сводные строки (по сотрудникам)
    operators_order: list[str] = []
    by_op: dict[str, list[dict]] = {}
    for r in detail_rows:
        op = r["ev"].operator_name
        if op not in by_op:
            by_op[op] = []
            operators_order.append(op)
        by_op[op].append(r)

    summary_rows = []
    for op in operators_order:
        op_rows = by_op[op]
        n = len(op_rows)

        if group_mode == "criteria":
            cells = []
            for i in range(len(all_ci)):
                applicable_vals = [r["cells"][i]["value"] for r in op_rows]
                yes_n = sum(1 for v in applicable_vals if v == "yes")
                app_n = sum(1 for v in applicable_vals if v in ("yes", "no"))
                pct = round(yes_n / app_n * 100, 1) if app_n > 0 else None
                pts = round(pct / 100 * all_ci[i]["crit"].weight, 1) if pct is not None else None
                cells.append({"pct": pct, "pts": pts})
        else:
            cells = []
            for i in range(len(blocks)):
                vals = [r["cells"][i]["pct"] for r in op_rows if r["cells"][i]["pct"] is not None]
                pct = _avg(vals)
                pts = round(pct / 100 * blocks[i].weight, 1) if pct is not None else None
                cells.append({"pct": pct, "pts": pts})

        totals = [r["total"] for r in op_rows if r["total"] is not None]
        won = sum(1 for r in op_rows if r["ev"].stage == WON)
        lost = sum(1 for r in op_rows if r["ev"].stage == LOST)
        closed = won + lost

        summary_rows.append({
            "name": op,
            "cells": cells,
            "total": _avg(totals),
            "count": n,
            "won_pct": round(won / closed * 100, 1) if closed > 0 else None,
        })

    # Строка «Команда»
    if group_mode == "criteria":
        team_cells = []
        for i in range(len(all_ci)):
            applicable_vals = [r["cells"][i]["value"] for r in detail_rows]
            yes_n = sum(1 for v in applicable_vals if v == "yes")
            app_n = sum(1 for v in applicable_vals if v in ("yes", "no"))
            pct = round(yes_n / app_n * 100, 1) if app_n > 0 else None
            pts = round(pct / 100 * all_ci[i]["crit"].weight, 1) if pct is not None else None
            team_cells.append({"pct": pct, "pts": pts})
    else:
        team_cells = []
        for i in range(len(blocks)):
            vals = [r["cells"][i]["pct"] for r in detail_rows if r["cells"][i]["pct"] is not None]
            pct = _avg(vals)
            pts = round(pct / 100 * blocks[i].weight, 1) if pct is not None else None
            team_cells.append({"pct": pct, "pts": pts})

    all_totals = [r["total"] for r in detail_rows if r["total"] is not None]
    team_won = sum(1 for r in detail_rows if r["ev"].stage == WON)
    team_lost = sum(1 for r in detail_rows if r["ev"].stage == LOST)
    team_closed = team_won + team_lost

    return {
        "group_mode": group_mode,
        "columns": columns,
        "column_groups": column_groups,
        "detail_rows": detail_rows,
        "summary_rows": summary_rows,
        "team_cells": team_cells,
        "team_total": _avg(all_totals),
        "team_count": len(detail_rows),
        "team_won_pct": round(team_won / team_closed * 100, 1) if team_closed > 0 else None,
    }


# ── Категория клиента vs Оценка ───────────────────────────────────────────────

def compute_category_score(rows: list[dict]) -> dict | None:
    """
    Для каждой категории клиента (A/B/C/D) вычисляет:
    - среднюю оценку
    - Win Rate (% успешных среди закрытых)
    - количество оценок
    Возвращает None, если нет оценок с категорией.
    """
    CAT_ORDER = ["A", "B", "C", "D"]
    by_cat: dict[str, list[dict]] = {c: [] for c in CAT_ORDER}

    for r in rows:
        cat = (r["ev"].client_category or "").strip().upper()
        if cat in by_cat and r["ev"].total_score is not None:
            by_cat[cat].append(r)

    result = []
    for cat in CAT_ORDER:
        cat_rows = by_cat[cat]
        if not cat_rows:
            continue
        scores = [float(r["ev"].total_score) for r in cat_rows]
        avg = round(sum(scores) / len(scores), 1)
        won   = sum(1 for r in cat_rows if r["ev"].stage == WON)
        lost  = sum(1 for r in cat_rows if r["ev"].stage == LOST)
        closed = won + lost
        wr = round(won / closed * 100, 1) if closed else None
        result.append({
            "category": cat,
            "count": len(cat_rows),
            "avg_score": avg,
            "win_rate": wr,
            "won": won,
            "lost": lost,
            "in_progress": sum(1 for r in cat_rows if r["ev"].stage not in (WON, LOST)),
        })

    return result if result else None


# ── Оценка vs Исход сделки ─────────────────────────────────────────────────────

def compute_score_outcome(rows: list[dict]) -> list[dict] | None:
    """
    Разбивает оценки на диапазоны и для каждого считает Won / Lost / В работе.
    """
    RANGES = [
        ("0–40%",   lambda s: s < 40),
        ("40–60%",  lambda s: 40 <= s < 60),
        ("60–80%",  lambda s: 60 <= s < 80),
        ("80–100%", lambda s: s >= 80),
    ]
    total_rows = [r for r in rows if r["ev"].total_score is not None]
    if not total_rows:
        return None

    result = []
    for label, fn in RANGES:
        subset = [r for r in total_rows if fn(float(r["ev"].total_score))]
        if not subset:
            continue
        won   = sum(1 for r in subset if r["ev"].stage == WON)
        lost  = sum(1 for r in subset if r["ev"].stage == LOST)
        prog  = sum(1 for r in subset if r["ev"].stage not in (WON, LOST))
        total = len(subset)
        closed = won + lost
        result.append({
            "range": label,
            "total": total,
            "won":   won,
            "lost":  lost,
            "in_progress": prog,
            "won_pct":  round(won  / total * 100, 1),
            "lost_pct": round(lost / total * 100, 1),
            "prog_pct": round(prog / total * 100, 1),
            "win_rate": round(won / closed * 100, 1) if closed else None,
        })
    return result if result else None


# ── Оценка vs Скорость закрытия ────────────────────────────────────────────────

def compute_closing_speed(rows: list[dict]) -> list[dict] | None:
    """
    Для каждого диапазона балла считает среднее количество дней
    от даты звонка (eval_date) до даты закрытия сделки (close_date из DealCache).
    Только для закрытых сделок (Won + Lost) с известными датами.
    """
    RANGES = [
        ("0–40%",   lambda s: s < 40),
        ("40–60%",  lambda s: 40 <= s < 60),
        ("60–80%",  lambda s: 60 <= s < 80),
        ("80–100%", lambda s: s >= 80),
    ]

    # Нужны строки с: total_score, close_date, eval_date, закрытая сделка
    eligible = [
        r for r in rows
        if r["ev"].total_score is not None
        and r.get("close_date") is not None
        and r["ev"].eval_date is not None
        and r["ev"].stage in (WON, LOST)
    ]
    if not eligible:
        return None

    def _days(r: dict) -> int | None:
        cd = r["close_date"]
        ed = r["ev"].eval_date
        if cd is None or ed is None:
            return None
        # ed может быть datetime или date
        ed_date = ed.date() if isinstance(ed, datetime) else ed
        cd_date = cd.date() if isinstance(cd, datetime) else cd
        delta = (cd_date - ed_date).days
        return delta if delta >= 0 else None   # отрицательные = артефакты данных

    result = []
    for label, fn in RANGES:
        subset = [r for r in eligible if fn(float(r["ev"].total_score))]
        if not subset:
            continue
        won_sub  = [r for r in subset if r["ev"].stage == WON]
        lost_sub = [r for r in subset if r["ev"].stage == LOST]

        won_days  = [d for r in won_sub  if (d := _days(r)) is not None]
        lost_days = [d for r in lost_sub if (d := _days(r)) is not None]
        all_days  = [d for r in subset   if (d := _days(r)) is not None]

        result.append({
            "range":     label,
            "n":         len(subset),
            "n_won":     len(won_sub),
            "n_lost":    len(lost_sub),
            "avg_days":  round(sum(all_days)  / len(all_days),  1) if all_days  else None,
            "avg_won":   round(sum(won_days)  / len(won_days),  1) if won_days  else None,
            "avg_lost":  round(sum(lost_days) / len(lost_days), 1) if lost_days else None,
        })

    return result if result else None


# ── План / Факт ────────────────────────────────────────────────────────────────

from calendar import monthrange
from collections import Counter as _Counter


def compute_plan_fact(evaluations: list, year: int, month: int, target_total: int) -> dict:
    days_in_month = monthrange(year, month)[1]
    by_emp: dict[str, dict] = {}

    for ev in evaluations:
        name = ev.operator_name
        if name not in by_emp:
            by_emp[name] = {"dept": ev.department, "by_day": _Counter()}
        if ev.eval_date:
            by_emp[name]["by_day"][ev.eval_date.day] += 1

    rows = []
    for name in sorted(by_emp.keys()):
        data = by_emp[name]
        cal = [data["by_day"].get(d, 0) for d in range(1, days_in_month + 1)]
        fact = sum(cal)
        rows.append({
            "name": name,
            "dept": data["dept"],
            "fact": fact,
            "days_count": sum(1 for c in cal if c > 0),
            "calendar": cal,
        })

    total_fact = sum(r["fact"] for r in rows)
    return {
        "rows": rows,
        "days_in_month": days_in_month,
        "days_labels": list(range(1, days_in_month + 1)),
        "total_fact": total_fact,
        "total_plan": target_total,
        "total_pct": round(total_fact / target_total * 100) if target_total else 0,
    }
