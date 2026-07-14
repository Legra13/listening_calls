import json
from calendar import monthrange
from collections import Counter
from datetime import date, datetime
from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, joinedload
from app.database import get_db
from app.models import (
    Block, Checklist, Criterion, Evaluation, EvaluationTarget, User,
    CalibrationSession, DealCache,
)
from app.deps import get_current_user, pop_flash
from app.analytics import (
    Filters, fetch_evaluations, get_filter_options,
    prep_rows, attach_close_dates,
    compute_kpi, compute_tab1, compute_tab2, compute_tab3,
    compute_category_score, compute_score_outcome, compute_closing_speed,
    heat_style, delta_style,
    EmployeeFilters, get_employee_report_options, fetch_evaluations_employee,
    compute_employee_report, compute_plan_fact,
)

router = APIRouter(prefix="/reports")
templates = Jinja2Templates(directory="app/templates")


@router.get("")
def reports_index(
    request: Request,
    checklist_id: str = "",
    departments: list[str] = Query(default=[]),
    operators: list[str] = Query(default=[]),
    date_from: str = "",
    date_to: str = "",
    stage: str = "",
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    options = get_filter_options(db)

    filters = Filters(
        departments=departments,
        operators=operators,
        date_from=date.fromisoformat(date_from) if date_from else None,
        date_to=date.fromisoformat(date_to) if date_to else None,
        checklist_id=int(checklist_id) if checklist_id else None,
        stage=stage if stage in ("won", "lost", "progress") else "",
    )

    evaluations = fetch_evaluations(db, filters)
    rows = prep_rows(evaluations)
    attach_close_dates(rows, db)

    # Определяем, какие чек-листы присутствуют в данных
    cl_counter = Counter(r["ev"].checklist_id for r in rows if r["ev"].checklist_id)
    available_cls: list[Checklist] = []
    if cl_counter:
        ids = [cid for cid, _ in cl_counter.most_common()]
        cl_map = {
            cl.id: cl
            for cl in db.query(Checklist)
            .options(joinedload(Checklist.blocks))
            .filter(Checklist.id.in_(ids))
            .all()
        }
        available_cls = [cl_map[i] for i in ids if i in cl_map]

    # Выбираем активный чек-лист
    selected_cl = None
    auto_cl = False
    if filters.checklist_id:
        selected_cl = next((cl for cl in available_cls if cl.id == filters.checklist_id), None)
        if not selected_cl and cl_counter:
            # запрошенный чек-лист не встречается в данных — берём из БД напрямую
            selected_cl = (
                db.query(Checklist)
                .options(joinedload(Checklist.blocks))
                .filter(Checklist.id == filters.checklist_id)
                .first()
            )
    elif available_cls:
        selected_cl = available_cls[0]  # самый частый
        auto_cl = len(available_cls) > 0

    # Фильтруем строки только по выбранному чек-листу
    rows_for_cl = (
        [r for r in rows if r["ev"].checklist_id == selected_cl.id]
        if selected_cl else []
    )

    kpi = compute_kpi(rows_for_cl)

    tab1 = tab2 = tab3 = None
    weekly_json = "[]"
    tab2_json = "[]"
    cat_score_json = "[]"
    score_outcome_json = "[]"
    closing_speed_json = "[]"

    if selected_cl and rows_for_cl:
        tab1 = compute_tab1(rows_for_cl, selected_cl)
        tab2 = compute_tab2(rows_for_cl, selected_cl)
        tab3 = compute_tab3(rows_for_cl, selected_cl)
        weekly_json = json.dumps(tab1["weekly"])
        tab2_json = json.dumps(tab2)

    # Эти три отчёта считаем по выбранному чек-листу, иначе по всем строкам
    base_rows = rows_for_cl if rows_for_cl else rows
    cat_score_data     = compute_category_score(base_rows)
    score_outcome_data = compute_score_outcome(base_rows)
    closing_speed_data = compute_closing_speed(base_rows)
    if cat_score_data:
        cat_score_json = json.dumps(cat_score_data)
    if score_outcome_data:
        score_outcome_json = json.dumps(score_outcome_data)
    if closing_speed_data:
        closing_speed_json = json.dumps(closing_speed_data)

    # ── Ключевой инсайт для шапки (серверная сторона) ─────────────────────────
    top_insight = None
    if score_outcome_data:
        _MIN = 5
        closed_ranges = [d for d in score_outcome_data
                         if d.get("won", 0) + d.get("lost", 0) >= _MIN]
        if len(closed_ranges) >= 2:
            first = closed_ranges[0]
            last  = closed_ranges[-1]
            wr1, wr2 = first.get("win_rate"), last.get("win_rate")
            if wr1 is not None and wr2 is not None and wr1 > 0:
                ratio = wr2 / wr1
                top_insight = {
                    "wr_low":      round(wr1, 1),
                    "wr_high":     round(wr2, 1),
                    "ratio":       round(ratio, 1),
                    "ratio_ok":    ratio >= 1.3,
                    "label_low":   first["range"],
                    "label_high":  last["range"],
                    "total_closed": sum(d.get("won", 0) + d.get("lost", 0)
                                       for d in score_outcome_data),
                    "total_all":   sum(d.get("total", 0) for d in score_outcome_data),
                }

    return templates.TemplateResponse("reports/index.html", {
        "request": request,
        "current_user": current_user,
        "flash": pop_flash(request),
        "options": options,
        "filters": filters,
        "stage": filters.stage,
        "kpi": kpi,
        "top_insight": top_insight,
        "selected_cl": selected_cl,
        "auto_cl": auto_cl,
        "available_cls": available_cls,
        "tab1": tab1,
        "tab2": tab2,
        "tab3": tab3,
        "weekly_json": weekly_json,
        "tab2_json": tab2_json,
        "cat_score_json": cat_score_json,
        "score_outcome_json": score_outcome_json,
        "closing_speed_json": closing_speed_json,
        "heat_style": heat_style,
        "delta_style": delta_style,
    })


@router.get("/employee")
def reports_employee(
    request: Request,
    checklist_id: str = "",
    departments: list[str] = Query(default=[]),
    operators: list[str] = Query(default=[]),
    call_date_from: str = "",
    call_date_to: str = "",
    rated_date_from: str = "",
    rated_date_to: str = "",
    evaluator_id: str = "",
    stage: str = "",
    client_category: list[str] = Query(default=[]),
    display_mode: str = "pct",
    group_mode: str = "groups",
    show_comments: str = "on",
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    options = get_employee_report_options(db)

    filters = EmployeeFilters(
        departments=departments,
        operators=operators,
        call_date_from=date.fromisoformat(call_date_from) if call_date_from else None,
        call_date_to=date.fromisoformat(call_date_to) if call_date_to else None,
        rated_date_from=date.fromisoformat(rated_date_from) if rated_date_from else None,
        rated_date_to=date.fromisoformat(rated_date_to) if rated_date_to else None,
        checklist_id=int(checklist_id) if checklist_id else None,
        evaluator_id=int(evaluator_id) if evaluator_id else None,
        stage=stage or None,
        client_category=client_category,
        display_mode=display_mode if display_mode in ("pct", "pts") else "pct",
        group_mode=group_mode if group_mode in ("groups", "criteria") else "groups",
        show_comments=(show_comments == "on"),
    )

    report = None
    selected_cl = None

    if filters.checklist_id:
        selected_cl = (
            db.query(Checklist)
            .options(
                joinedload(Checklist.blocks).joinedload(Block.criteria)
            )
            .filter(Checklist.id == filters.checklist_id)
            .first()
        )

    applied = bool(
        filters.checklist_id or filters.operators or filters.departments
        or filters.call_date_from or filters.call_date_to
        or filters.rated_date_from or filters.rated_date_to
    )

    duplicate_deal_ids: set[str] = set()
    if applied and selected_cl:
        evaluations = fetch_evaluations_employee(db, filters)
        if evaluations:
            report = compute_employee_report(evaluations, selected_cl, filters.group_mode)
            from collections import Counter
            deal_counts = Counter(ev.deal_id for ev in evaluations if ev.deal_id)
            duplicate_deal_ids = {did for did, cnt in deal_counts.items() if cnt > 1}

    return templates.TemplateResponse("reports/employee.html", {
        "request": request,
        "current_user": current_user,
        "flash": pop_flash(request),
        "options": options,
        "filters": filters,
        "selected_cl": selected_cl,
        "report": report,
        "applied": applied,
        "heat_style": heat_style,
        "BITRIX_BASE_URL": "https://entera.bitrix24.ru",
        "duplicate_deal_ids": duplicate_deal_ids,
    })


# ── Plan / Fact ────────────────────────────────────────────────────────────────

@router.get("/plan")
def reports_plan(
    request: Request,
    year: int = 0,
    month: int = 0,
    checklist_id: str = "",
    departments: list[str] = Query(default=[]),
    evaluator_id: str = "",
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    today = date.today()
    if not year:
        year = today.year
    if not month:
        month = today.month

    options = get_employee_report_options(db)
    cl_id = int(checklist_id) if checklist_id else None
    ev_id = int(evaluator_id) if evaluator_id else None

    # Load saved target for this period
    tq = db.query(EvaluationTarget).filter(
        EvaluationTarget.year == year,
        EvaluationTarget.month == month,
        EvaluationTarget.department.is_(None),
    )
    if cl_id:
        tq = tq.filter(EvaluationTarget.checklist_id == cl_id)
    else:
        tq = tq.filter(EvaluationTarget.checklist_id.is_(None))
    saved_target = tq.first()
    target_total = saved_target.target_per_employee if saved_target else 0

    # Fetch evaluations created in the selected month
    month_start = datetime(year, month, 1)
    last_day = monthrange(year, month)[1]
    month_end = datetime(year, month, last_day, 23, 59, 59)

    q = db.query(Evaluation).filter(
        Evaluation.status == "published",
        Evaluation.eval_date >= month_start,
        Evaluation.eval_date <= month_end,
    )
    if cl_id:
        q = q.filter(Evaluation.checklist_id == cl_id)
    if departments:
        q = q.filter(Evaluation.department.in_(departments))
    if ev_id:
        q = q.filter(Evaluation.evaluator_id == ev_id)

    evaluations = q.all()

    # Дата презентации из карточки сделки (DealCache) — для тепловой карты «дни презентаций»
    deal_ids = {ev.deal_id for ev in evaluations if ev.deal_id}
    pres_by_deal: dict[str, datetime] = {}
    if deal_ids:
        for dc in db.query(DealCache).filter(DealCache.deal_id.in_(deal_ids)).all():
            if dc.presentation_date:
                pres_by_deal[dc.deal_id] = dc.presentation_date
    pres_dates = {
        ev.id: pres_by_deal.get(ev.deal_id) for ev in evaluations if ev.deal_id
    }

    report = compute_plan_fact(evaluations, year, month, target_total, pres_dates)

    # Калибровки, завершённые (закрытые) за выбранный месяц
    cal_q = db.query(CalibrationSession).filter(CalibrationSession.status == "closed")
    if cl_id:
        cal_q = cal_q.filter(CalibrationSession.checklist_id == cl_id)
    calibrations_done = 0
    for sess in cal_q.all():
        d = sess.session_date or sess.updated_at or sess.created_at
        if d and d.year == year and d.month == month:
            calibrations_done += 1

    # Pace: expected fact by today within this month
    pace_target = None
    if year == today.year and month == today.month:
        elapsed = today.day
        pace_target = round(target_total * elapsed / last_day) if target_total else None

    return templates.TemplateResponse("reports/plan.html", {
        "request": request,
        "current_user": current_user,
        "flash": pop_flash(request),
        "options": options,
        "year": year,
        "month": month,
        "checklist_id": cl_id,
        "departments": departments,
        "target_total": target_total,
        "evaluator_id": ev_id,
        "report": report,
        "pace_target": pace_target,
        "calibrations_done": calibrations_done,
        "today_day": today.day if (year == today.year and month == today.month) else None,
    })


@router.post("/plan/target")
def save_plan_target(
    request: Request,
    year: int = Form(...),
    month: int = Form(...),
    checklist_id: str = Form(""),
    evaluator_id: str = Form(""),
    target_per_employee: int = Form(5),
    departments: list[str] = Form(default=[]),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    cl_id = int(checklist_id) if checklist_id else None

    existing = db.query(EvaluationTarget).filter(
        EvaluationTarget.year == year,
        EvaluationTarget.month == month,
        EvaluationTarget.department.is_(None),
        EvaluationTarget.checklist_id == cl_id if cl_id else EvaluationTarget.checklist_id.is_(None),
    ).first()

    if existing:
        existing.target_per_employee = target_per_employee  # хранит общий план
    else:
        db.add(EvaluationTarget(
            year=year,
            month=month,
            checklist_id=cl_id,
            target_per_employee=target_per_employee,
        ))  # поле target_per_employee переиспользуется как общий план
    db.commit()

    depts_qs = "&".join(f"departments={d}" for d in departments)
    redirect = f"/reports/plan?year={year}&month={month}&checklist_id={checklist_id or ''}&evaluator_id={evaluator_id or ''}"
    if depts_qs:
        redirect += f"&{depts_qs}"
    return RedirectResponse(redirect, status_code=303)
