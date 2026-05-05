"""
Аналитика звонков: агрегация данных для 3 вкладок отчёта.
Логика по logic_summary.md §§3–7.
"""
from __future__ import annotations
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime

from sqlalchemy.orm import Session, joinedload

from app.models import Block, Checklist, Criterion, Evaluation, EvaluationItem
from app.scoring import calculate_scores

WON = "сделка успешна"
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
    department: str = ""
    date_from: date | None = None
    date_to: date | None = None
    checklist_id: int | None = None


# ── Загрузка ─────────────────────────────────────────────────────────────────

def fetch_evaluations(db: Session, filters: Filters) -> list[Evaluation]:
    q = (
        db.query(Evaluation)
        .options(
            joinedload(Evaluation.items).joinedload(EvaluationItem.criterion),
            joinedload(Evaluation.checklist)
              .joinedload(Checklist.blocks)
              .joinedload(Block.criteria),
        )
    )
    if filters.department:
        q = q.filter(Evaluation.department == filters.department)
    if filters.operators:
        q = q.filter(Evaluation.operator_name.in_(filters.operators))
    if filters.date_from:
        q = q.filter(Evaluation.eval_date >= datetime.combine(filters.date_from, datetime.min.time()))
    if filters.date_to:
        q = q.filter(Evaluation.eval_date <= datetime.combine(filters.date_to, datetime.max.time()))
    if filters.checklist_id:
        q = q.filter(Evaluation.checklist_id == filters.checklist_id)
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

    dept_rows = (
        db.query(Evaluation.department)
        .filter(Evaluation.department.isnot(None), Evaluation.department != "")
        .distinct()
        .order_by(Evaluation.department)
        .all()
    )
    checklists = db.query(Checklist).filter(Checklist.status == "active").all()
    return {
        "operators": operators,
        "departments": [r[0] for r in dept_rows],
        "checklists": checklists,
    }


# ── Вспомогательные ────────────────────────────────────────────────────────────

def _avg(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 1) if values else None


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
        result.append({"ev": ev, "block_scores": block_scores})
    return result


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
            "won_pct": won_pct, "lost_pct": lost_pct}


# ── Tab 1 — Общие показатели ─────────────────────────────────────────────────

def compute_tab1(rows: list[dict], checklist: Checklist) -> dict:
    blocks = list(checklist.blocks)
    operators = sorted({r["ev"].operator_name for r in rows})

    hm_rows = []
    for op in operators:
        op_rows = [r for r in rows if r["ev"].operator_name == op]
        cells = []
        for block in blocks:
            vals = [r["block_scores"][block.id] for r in op_rows if block.id in r["block_scores"]]
            pct = _avg(vals)
            pts = round(pct / 100 * block.weight, 1) if pct is not None else None
            cells.append({"pct": pct, "pts": pts})
        total = _avg([r["ev"].total_score for r in op_rows if r["ev"].total_score is not None])
        won = sum(1 for r in op_rows if r["ev"].stage == WON)
        lost = sum(1 for r in op_rows if r["ev"].stage == LOST)
        closed = won + lost
        hm_rows.append({
            "name": op,
            "cells": cells,
            "total": total,
            "won_pct": round(won / closed * 100, 1) if closed else None,
            "lost_pct": round(lost / closed * 100, 1) if closed else None,
        })

    team_cells = []
    for block in blocks:
        vals = [r["block_scores"][block.id] for r in rows if block.id in r["block_scores"]]
        pct = _avg(vals)
        pts = round(pct / 100 * block.weight, 1) if pct is not None else None
        team_cells.append({"pct": pct, "pts": pts})
    team_total = _avg([r["ev"].total_score for r in rows if r["ev"].total_score is not None])
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
        won_vals = [float(r["block_scores"].get(bid, 0.0)) for r in won_rows]
        lost_vals = [float(r["block_scores"].get(bid, 0.0)) for r in lost_rows]
        avg_won = _avg(won_vals) if won_vals else None
        avg_lost = _avg(lost_vals) if lost_vals else None
        delta = round(avg_won - avg_lost, 1) if (avg_won is not None and avg_lost is not None) else None

        done = [r for r in rows if r["block_scores"].get(bid, 0.0) > 0]
        not_done = [r for r in rows if r["block_scores"].get(bid, 0.0) == 0.0]
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
            avg_won = _avg([float(r["block_scores"].get(bid, 0.0)) for r in won_r]) if won_r else None
            avg_lost = _avg([float(r["block_scores"].get(bid, 0.0)) for r in lost_r]) if lost_r else None
            delta = round(avg_won - avg_lost, 1) if (avg_won is not None and avg_lost is not None) else None
            cells.append({"won": avg_won, "lost": avg_lost, "delta": delta})
        return cells

    t1_rows = [
        {"name": op, "cells": _t1_cells([r for r in rows if r["ev"].operator_name == op])}
        for op in operators
    ]
    team_cells = _t1_cells(rows)

    RANGES = [
        ("0%",      lambda v: v is None or v == 0.0),
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
