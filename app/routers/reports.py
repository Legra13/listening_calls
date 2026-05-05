import json
from collections import Counter
from datetime import date
from fastapi import APIRouter, Depends, Query, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, joinedload
from app.database import get_db
from app.models import Checklist, User
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
    department: str = "",
    operators: list[str] = Query(default=[]),
    date_from: str = "",
    date_to: str = "",
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    options = get_filter_options(db)

    filters = Filters(
        department=department,
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
