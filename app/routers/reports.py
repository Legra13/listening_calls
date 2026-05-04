import json
from datetime import date
from fastapi import APIRouter, Depends, Query, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, joinedload
from app.database import get_db
from app.models import Block, Checklist, User
from app.deps import get_current_user, pop_flash
from app.analytics import (
    Filters, fetch_evaluations, get_filter_options,
    prep_rows, compute_kpi, compute_tab1, compute_tab2, compute_tab3,
    heat_style, delta_style,
)

router = APIRouter(prefix="/reports")
templates = Jinja2Templates(directory="app/templates")


@router.get("")
def reports_index(
    request: Request,
    checklist_id: str = "",
    operators: list[str] = Query(default=[]),
    date_from: str = "",
    date_to: str = "",
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    options = get_filter_options(db)

    filters = Filters(
        operators=operators,
        date_from=date.fromisoformat(date_from) if date_from else None,
        date_to=date.fromisoformat(date_to) if date_to else None,
        checklist_id=int(checklist_id) if checklist_id else None,
    )

    evaluations = fetch_evaluations(db, filters)
    rows = prep_rows(evaluations)
    kpi = compute_kpi(rows)

    selected_cl = None
    tab1 = tab2 = tab3 = None
    weekly_json = "[]"

    if filters.checklist_id:
        selected_cl = (
            db.query(Checklist)
            .options(joinedload(Checklist.blocks))
            .filter(Checklist.id == filters.checklist_id)
            .first()
        )

    if selected_cl and rows:
        tab1 = compute_tab1(rows, selected_cl)
        tab2 = compute_tab2(rows, selected_cl)
        tab3 = compute_tab3(rows, selected_cl)
        weekly_json = json.dumps(tab1["weekly"])

    return templates.TemplateResponse("reports/index.html", {
        "request": request,
        "current_user": current_user,
        "flash": pop_flash(request),
        "options": options,
        "filters": filters,
        "kpi": kpi,
        "selected_cl": selected_cl,
        "tab1": tab1,
        "tab2": tab2,
        "tab3": tab3,
        "weekly_json": weekly_json,
        "heat_style": heat_style,
        "delta_style": delta_style,
    })
