from datetime import datetime
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, joinedload
from app.database import get_db
from app.models import Block, Checklist, Criterion, Evaluation, EvaluationItem, User
from app.deps import get_current_user, flash, pop_flash
from app.scoring import calculate_scores, score_color, MONTH_NAMES

router = APIRouter(prefix="/evaluations")
templates = Jinja2Templates(directory="app/templates")


# ── List ──────────────────────────────────────────────────────────────────────

_SORT_COLUMNS = {
    "id":       Evaluation.id,
    "operator": Evaluation.operator_name,
    "date":     Evaluation.eval_date,
    "score":    Evaluation.total_score,
    "stage":    Evaluation.stage,
    "dept":     Evaluation.department,
}


@router.get("")
def evaluations_index(
    request: Request,
    operator: str = "",
    checklist_id: str = "",
    stage: str = "",
    department: str = "",
    date_from: str = "",
    date_to: str = "",
    sort: str = "id",
    dir: str = "desc",
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    q = (
        db.query(Evaluation)
        .options(joinedload(Evaluation.checklist), joinedload(Evaluation.evaluator))
    )
    if operator:
        q = q.filter(Evaluation.operator_name.ilike(f"%{operator}%"))
    if checklist_id:
        q = q.filter(Evaluation.checklist_id == int(checklist_id))
    if stage:
        q = q.filter(Evaluation.stage == stage)
    if department:
        q = q.filter(Evaluation.department == department)
    if date_from:
        try:
            q = q.filter(Evaluation.eval_date >= datetime.strptime(date_from, "%Y-%m-%d").date())
        except ValueError:
            pass
    if date_to:
        try:
            q = q.filter(Evaluation.eval_date <= datetime.strptime(date_to, "%Y-%m-%d").date())
        except ValueError:
            pass

    col = _SORT_COLUMNS.get(sort, Evaluation.id)
    q = q.order_by(col.asc() if dir == "asc" else col.desc())

    evaluations = q.limit(200).all()
    checklists = db.query(Checklist).all()

    dept_rows = (
        db.query(Evaluation.department)
        .filter(Evaluation.department.isnot(None), Evaluation.department != "")
        .distinct()
        .order_by(Evaluation.department)
        .all()
    )
    departments = [r[0] for r in dept_rows]

    operator_rows = (
        db.query(Evaluation.operator_name)
        .filter(Evaluation.operator_name.isnot(None), Evaluation.operator_name != "")
        .distinct()
        .order_by(Evaluation.operator_name)
        .all()
    )
    known_operators = [r[0] for r in operator_rows]

    return templates.TemplateResponse("evaluations/index.html", {
        "request": request,
        "current_user": current_user,
        "evaluations": evaluations,
        "checklists": checklists,
        "departments": departments,
        "known_operators": known_operators,
        "filters": {
            "operator": operator,
            "checklist_id": checklist_id,
            "stage": stage,
            "department": department,
            "date_from": date_from,
            "date_to": date_to,
        },
        "sort": sort,
        "dir": dir,
        "score_color": score_color,
        "flash": pop_flash(request),
    })


# ── New / Create ──────────────────────────────────────────────────────────────

@router.get("/new")
def evaluations_new(
    request: Request,
    checklist_id: int | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    checklists = db.query(Checklist).filter(Checklist.status == "active").all()
    selected_cl = None
    if checklist_id:
        selected_cl = (
            db.query(Checklist)
            .options(joinedload(Checklist.blocks).joinedload(Block.criteria))
            .filter(Checklist.id == checklist_id)
            .first()
        )
    return templates.TemplateResponse("evaluations/new.html", {
        "request": request,
        "current_user": current_user,
        "checklists": checklists,
        "selected_cl": selected_cl,
        "flash": pop_flash(request),
    })


@router.post("")
async def evaluations_create(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    form = await request.form()

    checklist_id = int(form.get("checklist_id", 0))
    deal_id = (form.get("deal_id") or "").strip()
    operator_name = (form.get("operator_name") or "").strip()
    department = (form.get("department") or "").strip()
    eval_date_str = (form.get("eval_date") or "").strip()
    stage = (form.get("stage") or "в работе").strip()
    general_comment = (form.get("general_comment") or "").strip()

    if not operator_name:
        flash(request, "Укажите имя оператора", "danger")
        return RedirectResponse(f"/evaluations/new?checklist_id={checklist_id}", status_code=302)

    cl = (
        db.query(Checklist)
        .options(joinedload(Checklist.blocks).joinedload(Block.criteria))
        .filter(Checklist.id == checklist_id)
        .first()
    )
    if not cl:
        flash(request, "Чек-лист не найден", "danger")
        return RedirectResponse("/evaluations/new", status_code=302)

    # Parse date
    eval_date: datetime | None = None
    if eval_date_str:
        try:
            eval_date = datetime.strptime(eval_date_str, "%Y-%m-%d")
        except ValueError:
            pass

    # Collect criterion values from form
    items_raw: list[tuple[int, str, str]] = []
    all_criteria = [c for block in cl.blocks for c in block.criteria]
    for crit in all_criteria:
        value = str(form.get(f"criterion_{crit.id}", "na"))
        score_type = getattr(crit, 'score_type', 'binary')
        if score_type == 'range':
            if value != 'na':
                try:
                    score_max = getattr(crit, 'score_max', 5) or 5
                    v = int(value)
                    if not (1 <= v <= score_max):
                        value = "na"
                except (ValueError, TypeError):
                    value = "na"
        else:
            if value not in ("yes", "no", "na"):
                value = "na"
        comment = (form.get(f"comment_{crit.id}") or "").strip()
        items_raw.append((crit.id, value, comment))

    total_score, _ = calculate_scores(items_raw, cl)

    evaluation = Evaluation(
        checklist_id=checklist_id,
        deal_id=deal_id or None,
        operator_name=operator_name,
        department=department or None,
        eval_date=eval_date,
        week_num=eval_date.isocalendar()[1] if eval_date else None,
        week_year=eval_date.year if eval_date else None,
        month=MONTH_NAMES[eval_date.month - 1] if eval_date else None,
        stage=stage,
        total_score=total_score,
        evaluator_id=current_user.id,
        general_comment=general_comment or None,
    )
    db.add(evaluation)
    db.flush()

    for crit_id, value, comment in items_raw:
        db.add(EvaluationItem(
            evaluation_id=evaluation.id,
            criterion_id=crit_id,
            value=value,
            comment=comment or None,
        ))

    db.commit()
    flash(request, f"Оценка сохранена. Итог: {total_score:.1f}%")
    return RedirectResponse(f"/evaluations/{evaluation.id}", status_code=302)


# ── View ──────────────────────────────────────────────────────────────────────

@router.get("/{eval_id}")
def evaluations_view(
    eval_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    evaluation = (
        db.query(Evaluation)
        .options(
            joinedload(Evaluation.checklist).joinedload(Checklist.blocks).joinedload(Block.criteria),
            joinedload(Evaluation.evaluator),
            joinedload(Evaluation.items).joinedload(EvaluationItem.criterion),
        )
        .filter(Evaluation.id == eval_id)
        .first()
    )
    if not evaluation:
        flash(request, "Оценка не найдена", "danger")
        return RedirectResponse("/evaluations", status_code=302)

    item_map = {item.criterion_id: item for item in evaluation.items}
    _, block_scores = calculate_scores(evaluation.items, evaluation.checklist)

    return templates.TemplateResponse("evaluations/view.html", {
        "request": request,
        "current_user": current_user,
        "ev": evaluation,
        "item_map": item_map,
        "block_scores": block_scores,
        "score_color": score_color,
        "flash": pop_flash(request),
    })


# ── Edit ─────────────────────────────────────────────────────────────────────

@router.get("/{eval_id}/edit")
def evaluations_edit(
    eval_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    evaluation = (
        db.query(Evaluation)
        .options(
            joinedload(Evaluation.checklist).joinedload(Checklist.blocks).joinedload(Block.criteria),
            joinedload(Evaluation.items),
        )
        .filter(Evaluation.id == eval_id)
        .first()
    )
    if not evaluation:
        flash(request, "Оценка не найдена", "danger")
        return RedirectResponse("/evaluations", status_code=302)

    item_map = {item.criterion_id: item for item in evaluation.items}
    checklists = db.query(Checklist).filter(Checklist.status == "active").all()

    return templates.TemplateResponse("evaluations/edit.html", {
        "request": request,
        "current_user": current_user,
        "ev": evaluation,
        "selected_cl": evaluation.checklist,
        "checklists": checklists,
        "item_map": item_map,
        "flash": pop_flash(request),
    })


@router.post("/{eval_id}/update")
async def evaluations_update(
    eval_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    evaluation = db.query(Evaluation).filter(Evaluation.id == eval_id).first()
    if not evaluation:
        return RedirectResponse("/evaluations", status_code=302)

    form = await request.form()
    deal_id = (form.get("deal_id") or "").strip()
    operator_name = (form.get("operator_name") or "").strip()
    department = (form.get("department") or "").strip()
    eval_date_str = (form.get("eval_date") or "").strip()
    stage = (form.get("stage") or "в работе").strip()
    general_comment = (form.get("general_comment") or "").strip()

    eval_date: datetime | None = None
    if eval_date_str:
        try:
            eval_date = datetime.strptime(eval_date_str, "%Y-%m-%d")
        except ValueError:
            pass

    evaluation.deal_id = deal_id or None
    evaluation.operator_name = operator_name
    evaluation.department = department or None
    evaluation.eval_date = eval_date
    evaluation.week_num = eval_date.isocalendar()[1] if eval_date else None
    evaluation.week_year = eval_date.year if eval_date else None
    evaluation.month = MONTH_NAMES[eval_date.month - 1] if eval_date else None
    evaluation.stage = stage
    evaluation.general_comment = general_comment or None

    # Update items
    cl = (
        db.query(Checklist)
        .options(joinedload(Checklist.blocks).joinedload(Block.criteria))
        .filter(Checklist.id == evaluation.checklist_id)
        .first()
    )
    all_criteria = [c for block in cl.blocks for c in block.criteria]
    items_raw: list[tuple[int, str, str]] = []

    for crit in all_criteria:
        value = str(form.get(f"criterion_{crit.id}", "na"))
        score_type = getattr(crit, 'score_type', 'binary')
        if score_type == 'range':
            if value != 'na':
                try:
                    score_max = getattr(crit, 'score_max', 5) or 5
                    v = int(value)
                    if not (1 <= v <= score_max):
                        value = "na"
                except (ValueError, TypeError):
                    value = "na"
        else:
            if value not in ("yes", "no", "na"):
                value = "na"
        comment = (form.get(f"comment_{crit.id}") or "").strip()
        items_raw.append((crit.id, value, comment))

    total_score, _ = calculate_scores(items_raw, cl)
    evaluation.total_score = total_score
    evaluation.updated_at = datetime.utcnow()

    # Replace items
    db.query(EvaluationItem).filter(EvaluationItem.evaluation_id == eval_id).delete()
    for crit_id, value, comment in items_raw:
        db.add(EvaluationItem(
            evaluation_id=eval_id,
            criterion_id=crit_id,
            value=value,
            comment=comment or None,
        ))

    db.commit()
    flash(request, f"Оценка обновлена. Итог: {total_score:.1f}%")
    return RedirectResponse(f"/evaluations/{eval_id}", status_code=302)


# ── Delete ────────────────────────────────────────────────────────────────────

@router.post("/{eval_id}/delete")
def evaluations_delete(
    eval_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    evaluation = db.query(Evaluation).filter(Evaluation.id == eval_id).first()
    if evaluation:
        db.delete(evaluation)
        db.commit()
        flash(request, "Оценка удалена")
    return RedirectResponse("/evaluations", status_code=302)
