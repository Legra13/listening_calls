import json
from calendar import monthrange
from collections import Counter
from datetime import date, datetime
from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, joinedload
from app.database import get_db
from app.models import Block, Checklist, Criterion, Evaluation, EvaluationTarget, User
from app.deps import get_current_user, pop_flash
from app.analytics import (
    Filters, fetch_evaluations, get_filter_options,
    prep_rows, compute_kpi, compute_tab1, compute_tab2, compute_tab3,
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
    )

    evaluations = fetch_evaluations(db, filters)
    rows = prep_rows(evaluations)

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

    if selected_cl and rows_for_cl:
        tab1 = compute_tab1(rows_for_cl, selected_cl)
        tab2 = compute_tab2(rows_for_cl, selected_cl)
        tab3 = compute_tab3(rows_for_cl, selected_cl)
        weekly_json = json.dumps(tab1["weekly"])
        tab2_json = json.dumps(tab2)

    return templates.TemplateResponse("reports/index.html", {
        "request": request,
        "current_user": current_user,
        "flash": pop_flash(request),
        "options": options,
        "filters": filters,
        "kpi": kpi,
        "selected_cl": selected_cl,
        "auto_cl": auto_cl,
        "available_cls": available_cls,
        "tab1": tab1,
        "tab2": tab2,
        "tab3": tab3,
        "weekly_json": weekly_json,
        "tab2_json": tab2_json,
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
    client_category: str = "",
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
        client_category=client_category or None,
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
    target_per_employee = saved_target.target_per_employee if saved_target else 5

    # Fetch evaluations created in the selected month
    month_start = datetime(year, month, 1)
    last_day = monthrange(year, month)[1]
    month_end = datetime(year, month, last_day, 23, 59, 59)

    q = db.query(Evaluation).filter(
        Evaluation.status == "published",
        Evaluation.created_at >= month_start,
        Evaluation.created_at <= month_end,
    )
    if cl_id:
        q = q.filter(Evaluation.checklist_id == cl_id)
    if departments:
        q = q.filter(Evaluation.department.in_(departments))

    evaluations = q.all()
    report = compute_plan_fact(evaluations, year, month, target_per_employee)

    # Pace: expected fact by today within this month
    pace_target = None
    if year == today.year and month == today.month:
        elapsed = today.day
        pace_target = round(target_per_employee * elapsed / last_day)

    return templates.TemplateResponse("reports/plan.html", {
        "request": request,
        "current_user": current_user,
        "flash": pop_flash(request),
        "options": options,
        "year": year,
        "month": month,
        "checklist_id": cl_id,
        "departments": departments,
        "target_per_employee": target_per_employee,
        "report": report,
        "pace_target": pace_target,
        "today_day": today.day if (year == today.year and month == today.month) else None,
    })


@router.post("/plan/target")
def save_plan_target(
    request: Request,
    year: int = Form(...),
    month: int = Form(...),
    checklist_id: str = Form(""),
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
        existing.target_per_employee = target_per_employee
    else:
        db.add(EvaluationTarget(
            year=year,
            month=month,
            checklist_id=cl_id,
            target_per_employee=target_per_employee,
        ))
    db.commit()

    depts_qs = "&".join(f"departments={d}" for d in departments)
    redirect = f"/reports/plan?year={year}&month={month}&checklist_id={checklist_id or ''}"
    if depts_qs:
        redirect += f"&{depts_qs}"
    return RedirectResponse(redirect, status_code=303)
