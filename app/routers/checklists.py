from typing import List

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import Block, Checklist, Criterion, User
from app.deps import get_current_user, flash, pop_flash

router = APIRouter(prefix="/checklists")
templates = Jinja2Templates(directory="app/templates")


# ── Helpers ──────────────────────────────────────────────────────────────────

def _sync_block_weight(db: Session, block_id: int) -> None:
    """Пересчитывает вес блока как сумму весов его критериев."""
    from sqlalchemy import func
    total = db.query(func.sum(Criterion.weight)).filter(Criterion.block_id == block_id).scalar() or 0
    db.query(Block).filter(Block.id == block_id).update({"weight": total})
    db.commit()


def _get_checklist_or_404(db: Session, checklist_id: int) -> Checklist:
    from fastapi import HTTPException
    cl = db.query(Checklist).filter(Checklist.id == checklist_id).first()
    if not cl:
        raise HTTPException(status_code=404, detail="Форма оценки не найдена")
    return cl


def _redirect_edit(checklist_id: int, anchor: str = "") -> RedirectResponse:
    url = f"/checklists/{checklist_id}/edit"
    if anchor:
        url += f"#{anchor}"
    return RedirectResponse(url, status_code=302)


# ── Checklist CRUD ────────────────────────────────────────────────────────────

@router.get("")
def checklists_index(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    checklists = (
        db.query(Checklist)
        .order_by(Checklist.created_at.desc())
        .all()
    )
    return templates.TemplateResponse("checklists/index.html", {
        "request": request,
        "current_user": current_user,
        "checklists": checklists,
        "flash": pop_flash(request),
    })


@router.get("/new")
def checklists_new(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    return templates.TemplateResponse("checklists/new.html", {
        "request": request,
        "current_user": current_user,
        "flash": pop_flash(request),
    })


@router.post("")
def checklists_create(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    calculation: str = Form("average"),
    autofail_enabled: str = Form(""),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    cl = Checklist(
        name=name,
        description=description or None,
        created_by_id=current_user.id,
        status="draft",
        calculation=calculation if calculation in ("weighted", "average") else "average",
        autofail_enabled=bool(autofail_enabled),
    )
    db.add(cl)
    db.commit()
    db.refresh(cl)
    flash(request, f"Форма оценки «{cl.name}» создана")
    return RedirectResponse(f"/checklists/{cl.id}/edit", status_code=302)


@router.get("/{checklist_id}/settings")
def checklists_settings(
    checklist_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    cl = _get_checklist_or_404(db, checklist_id)
    selected_depts = [d.strip() for d in (cl.departments or "").split(",") if d.strip()]
    return templates.TemplateResponse("checklists/settings.html", {
        "request": request,
        "current_user": current_user,
        "cl": cl,
        "selected_depts": selected_depts,
        "flash": pop_flash(request),
    })


@router.post("/{checklist_id}/settings")
async def checklists_settings_update(
    checklist_id: int,
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    status: str = Form("draft"),
    autofail_enabled: str = Form(""),
    calculation: str = Form("average"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    form = await request.form()
    dept_values = form.getlist("departments")
    cl = _get_checklist_or_404(db, checklist_id)
    cl.name = name
    cl.description = description or None
    cl.status = status if status in ("draft", "active", "archived") else "draft"
    cl.is_active = (cl.status == "active")
    cl.autofail_enabled = bool(autofail_enabled)
    cl.calculation = calculation if calculation in ("weighted", "average") else "average"
    cl.departments = ",".join(dept_values) if dept_values else None
    db.commit()
    flash(request, "Настройки сохранены")
    return RedirectResponse(f"/checklists/{checklist_id}/edit", status_code=302)


@router.get("/{checklist_id}/edit")
def checklists_edit(
    checklist_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    cl = _get_checklist_or_404(db, checklist_id)
    return templates.TemplateResponse("checklists/edit.html", {
        "request": request,
        "current_user": current_user,
        "cl": cl,
        "flash": pop_flash(request),
    })


@router.post("/{checklist_id}")
def checklists_update(
    checklist_id: int,
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    cl = _get_checklist_or_404(db, checklist_id)
    cl.name = name
    cl.description = description or None
    db.commit()
    flash(request, "Сохранено")
    return _redirect_edit(checklist_id)


@router.post("/{checklist_id}/publish")
def checklists_publish(
    checklist_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    cl = _get_checklist_or_404(db, checklist_id)
    cl.status = "active"
    cl.is_active = True
    db.commit()
    flash(request, f"Форма оценки «{cl.name}» опубликована")
    return _redirect_edit(checklist_id)


@router.post("/{checklist_id}/delete")
def checklists_delete(
    checklist_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from app.models import Evaluation
    cl = _get_checklist_or_404(db, checklist_id)
    eval_count = db.query(Evaluation).filter(Evaluation.checklist_id == checklist_id).count()
    if eval_count > 0:
        flash(request, f"Нельзя удалить: к форме привязано {eval_count} оценок. Сначала архивируйте её.", "danger")
        return RedirectResponse("/checklists", status_code=302)
    name = cl.name
    db.delete(cl)
    db.commit()
    flash(request, f"Форма оценки «{name}» удалена")
    return RedirectResponse("/checklists", status_code=302)


@router.post("/{checklist_id}/archive")
def checklists_archive(
    checklist_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    cl = _get_checklist_or_404(db, checklist_id)
    if cl.status == "archived":
        cl.status = "active"
        cl.is_active = True
        msg = f"Форма оценки «{cl.name}» восстановлена"
    else:
        cl.status = "archived"
        cl.is_active = False
        msg = f"Форма оценки «{cl.name}» архивирована"
    db.commit()
    flash(request, msg)
    return RedirectResponse("/checklists", status_code=302)


# ── Block CRUD ────────────────────────────────────────────────────────────────

@router.post("/{checklist_id}/blocks")
def blocks_create(
    checklist_id: int,
    request: Request,
    display_name: str = Form(...),
    weight: int = Form(0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _get_checklist_or_404(db, checklist_id)
    max_order = db.query(Block).filter(Block.checklist_id == checklist_id).count()
    block = Block(
        checklist_id=checklist_id,
        name=display_name,
        display_name=display_name,
        weight=weight,
        order_index=max_order,
    )
    db.add(block)
    db.commit()
    db.refresh(block)
    return _redirect_edit(checklist_id, anchor=f"block-{block.id}")


@router.post("/{checklist_id}/blocks/{block_id}")
def blocks_update(
    checklist_id: int,
    block_id: int,
    request: Request,
    display_name: str = Form(...),
    weight: int = Form(0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    block = db.query(Block).filter(Block.id == block_id, Block.checklist_id == checklist_id).first()
    if block:
        block.display_name = display_name
        block.name = display_name
        block.weight = weight
        db.commit()
    return _redirect_edit(checklist_id, anchor=f"block-{block_id}")


@router.post("/{checklist_id}/blocks/{block_id}/delete")
def blocks_delete(
    checklist_id: int,
    block_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    block = db.query(Block).filter(Block.id == block_id, Block.checklist_id == checklist_id).first()
    if block:
        db.delete(block)
        db.commit()
        flash(request, f"Группа «{block.display_name or block.name}» удалена")
    return _redirect_edit(checklist_id)


# ── Criterion CRUD ────────────────────────────────────────────────────────────

@router.post("/{checklist_id}/blocks/{block_id}/criteria")
def criteria_create(
    checklist_id: int,
    block_id: int,
    request: Request,
    text: str = Form(...),
    description: str = Form(""),
    weight: int = Form(1),
    is_autofail: str = Form(""),
    score_type: str = Form("binary"),
    score_max: int = Form(5),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    block = db.query(Block).filter(Block.id == block_id, Block.checklist_id == checklist_id).first()
    if block:
        max_order = len(block.criteria)
        crit = Criterion(
            block_id=block_id, text=text,
            description=description or None,
            weight=weight,
            is_autofail=bool(is_autofail),
            order_index=max_order,
        )
        crit.score_type = score_type if score_type in ("binary", "range") else "binary"
        crit.score_max = max(1, min(10, score_max))
        db.add(crit)
        db.commit()
        _sync_block_weight(db, block_id)
    return _redirect_edit(checklist_id, anchor=f"block-{block_id}")


@router.post("/{checklist_id}/blocks/{block_id}/criteria-from-library")
def criteria_from_library(
    checklist_id: int,
    block_id: int,
    request: Request,
    crit_ids: List[int] = Form(default=[]),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    block = db.query(Block).filter(Block.id == block_id, Block.checklist_id == checklist_id).first()
    if block:
        for crit_id in crit_ids:
            source = db.query(Criterion).filter(Criterion.id == crit_id).first()
            if source:
                crit = Criterion(
                    block_id=block_id,
                    text=source.text,
                    description=source.description,
                    weight=source.weight,
                    is_autofail=False,
                    order_index=len(block.criteria),
                )
                db.add(crit)
                db.flush()
        db.commit()
        _sync_block_weight(db, block_id)
    return _redirect_edit(checklist_id, anchor=f"block-{block_id}")


@router.post("/{checklist_id}/blocks/{block_id}/criteria/{crit_id}")
def criteria_update(
    checklist_id: int,
    block_id: int,
    crit_id: int,
    request: Request,
    text: str = Form(...),
    description: str = Form(""),
    weight: int = Form(1),
    is_autofail: str = Form(""),
    score_type: str = Form("binary"),
    score_max: int = Form(5),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    crit = db.query(Criterion).filter(Criterion.id == crit_id, Criterion.block_id == block_id).first()
    if crit:
        crit.text = text
        crit.description = description or None
        crit.weight = weight
        crit.is_autofail = bool(is_autofail)
        crit.score_type = score_type if score_type in ("binary", "range") else "binary"
        crit.score_max = max(1, min(10, score_max))
        db.commit()
        _sync_block_weight(db, block_id)
    return _redirect_edit(checklist_id, anchor=f"block-{block_id}")


@router.post("/{checklist_id}/blocks/{block_id}/criteria/{crit_id}/delete")
def criteria_delete(
    checklist_id: int,
    block_id: int,
    crit_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    crit = db.query(Criterion).filter(Criterion.id == crit_id, Criterion.block_id == block_id).first()
    if crit:
        db.delete(crit)
        db.commit()
        _sync_block_weight(db, block_id)
    return _redirect_edit(checklist_id, anchor=f"block-{block_id}")
