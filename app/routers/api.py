"""
Внутреннее API для HTMX-запросов.
"""
import os
import subprocess
import threading
from datetime import datetime
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import Block, Checklist, Criterion, DealCache, User
from app.deps import get_current_user
from app.bitrix import get_deal, get_employees, get_departments, DealInfo

router = APIRouter(prefix="/api")

STAGE_BADGE = {
    "сделка успешна": ("success", "Успешна"),
    "не смог продать": ("danger", "Не продал"),
    "в работе": ("warning", "В работе"),
}


@router.get("/deal/{deal_id}")
def deal_lookup(
    deal_id: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Ищет сделку в кеше, при промахе — запрашивает Битрикс.
    Возвращает JSON с данными для автозаполнения формы оценки.
    """
    # Кеш
    # Порог: записи до этой даты закешированы без presentation_date → нужно обновить
    _PDATE_MIGRATION = datetime(2026, 5, 21, 12, 0, 0)

    cached = db.query(DealCache).filter(DealCache.deal_id == deal_id).first()
    if cached:
        pdate_stale = (
            cached.presentation_date is None
            and cached.last_synced_at is not None
            and cached.last_synced_at < _PDATE_MIGRATION
        )
        if not pdate_stale:
            return _deal_response(
                deal_id=deal_id,
                operator_name=cached.operator_name or "",
                department=cached.department,
                deal_date=cached.deal_date,
                stage=cached.stage or "в работе",
                from_cache=True,
                presentation_date=cached.presentation_date,
            )

    # Битрикс
    try:
        info: DealInfo | None = get_deal(deal_id)
    except ConnectionError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)

    if not info:
        return JSONResponse({"error": f"Сделка #{deal_id} не найдена в Битрикс"}, status_code=404)

    # Сохраняем в кеш
    entry = DealCache(
        deal_id=deal_id,
        operator_name=info.operator_name,
        department=info.department,
        deal_date=info.deal_date,
        presentation_date=info.presentation_date,
        stage=info.stage,
        last_synced_at=datetime.utcnow(),
    )
    db.merge(entry)
    db.commit()

    return _deal_response(
        deal_id=deal_id,
        operator_name=info.operator_name,
        department=info.department,
        deal_date=info.deal_date,
        stage=info.stage,
        from_cache=False,
        presentation_date=info.presentation_date,
    )


@router.get("/criteria-library")
def criteria_library(
    checklist_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rows = (
        db.query(Criterion, Block, Checklist)
        .join(Block, Criterion.block_id == Block.id)
        .join(Checklist, Block.checklist_id == Checklist.id)
        .filter(Block.checklist_id != checklist_id)
        .order_by(Checklist.name, Block.order_index, Criterion.order_index)
        .all()
    )
    result = [
        {
            "id": crit.id,
            "text": crit.text,
            "description": crit.description or "",
            "weight": crit.weight,
            "checklist_name": cl.name,
        }
        for crit, block, cl in rows
    ]
    return JSONResponse(result)


_emp_cache: list[dict] = []
_emp_cached_at: float = 0.0
_dept_cache: list[dict] = []
_dept_cached_at: float = 0.0


@router.get("/departments")
def departments_list(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    import time
    global _dept_cache, _dept_cached_at
    if not _dept_cache or (time.time() - _dept_cached_at) > 3600:
        _dept_cache = get_departments()
        _dept_cached_at = time.time()
    return JSONResponse(_dept_cache)


@router.get("/employees")
def employees_list(
    request: Request,
    department: str = "",
    current_user: User = Depends(get_current_user),
):
    import time
    global _emp_cache, _emp_cached_at
    if not _emp_cache or (time.time() - _emp_cached_at) > 3600:
        _emp_cache = get_employees()
        _emp_cached_at = time.time()
    if department:
        return JSONResponse([e for e in _emp_cache if e.get("department") == department])
    return JSONResponse(_emp_cache)


def _deal_response(
    deal_id: str,
    operator_name: str,
    department: str | None,
    deal_date: datetime | None,
    stage: str,
    from_cache: bool,
    presentation_date=None,
) -> JSONResponse:
    badge_class, badge_label = STAGE_BADGE.get(stage, ("secondary", stage))
    pd_str = ""
    if presentation_date is not None:
        try:
            pd_str = presentation_date.strftime("%Y-%m-%d")
        except AttributeError:
            pd_str = str(presentation_date)[:10]
    return JSONResponse({
        "deal_id": deal_id,
        "operator_name": operator_name,
        "department": department or "",
        "deal_date": deal_date.strftime("%Y-%m-%d") if deal_date else "",
        "presentation_date": pd_str,
        "stage": stage,
        "stage_badge": badge_class,
        "stage_label": badge_label,
        "from_cache": from_cache,
    })


class ReorderPayload(BaseModel):
    ids: list[int]


@router.post("/checklists/reorder")
def checklists_reorder(
    payload: ReorderPayload,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    for idx, cl_id in enumerate(payload.ids):
        db.query(Checklist).filter(Checklist.id == cl_id).update({"order_index": idx})
    db.commit()
    return {"ok": True}


DEPLOY_SECRET = os.getenv("DEPLOY_SECRET", "entera-deploy-2025")

@router.post("/deploy")
def deploy(request: Request):
    token = request.headers.get("X-Deploy-Token", "")
    if token != DEPLOY_SECRET:
        from fastapi import HTTPException
        raise HTTPException(status_code=403)

    def run():
        import time, os
        time.sleep(1)
        app_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        subprocess.run(["git", "-C", app_dir, "pull", "origin", "main"],
                       capture_output=True)
        subprocess.run(["sudo", "systemctl", "restart", "call-eval"],
                       capture_output=True)

    threading.Thread(target=run, daemon=True).start()
    return {"status": "deploying"}


@router.get("/admin/eval-detail")
def eval_detail(
    request: Request,
    db: Session = Depends(get_db),
):
    token = request.headers.get("X-Deploy-Token", "")
    if token != DEPLOY_SECRET:
        from fastapi import HTTPException
        raise HTTPException(status_code=403)
    from sqlalchemy import text
    rows = db.execute(text(
        "SELECT e.id, e.deal_id, e.operator_name, e.department, e.eval_date, "
        "e.updated_at, e.stage, e.total_score, e.status, e.general_comment, "
        "e.evaluator_id, e.created_at "
        "FROM evaluations e WHERE e.checklist_id=9 AND e.id BETWEEN 693 AND 729 "
        "ORDER BY e.id"
    )).fetchall()
    evals = []
    for r in rows:
        items = db.execute(text(
            "SELECT ei.criterion_id, ei.value, ei.comment "
            "FROM evaluation_items ei WHERE ei.evaluation_id=:eid ORDER BY ei.criterion_id"
        ), {"eid": r[0]}).fetchall()
        evals.append({
            "id": r[0], "deal_id": r[1], "operator": r[2], "dept": r[3],
            "eval_date": str(r[4]), "updated_at": str(r[5]), "stage": r[6],
            "score": r[7], "status": r[8], "general_comment": r[9],
            "evaluator_id": r[10], "created_at": str(r[11]),
            "items": [{"crit_id": i[0], "value": i[1], "comment": i[2]} for i in items],
        })
    return evals


@router.post("/admin/fix-quotes")
def fix_quotes(
    request: Request,
    db: Session = Depends(get_db),
):
    """Заменяет &quot; на " в комментариях оценок чек-листа 9."""
    token = request.headers.get("X-Deploy-Token", "")
    if token != DEPLOY_SECRET:
        from fastapi import HTTPException
        raise HTTPException(status_code=403)
    import html as _html
    from sqlalchemy import text
    items = db.execute(text(
        "SELECT ei.id, ei.comment FROM evaluation_items ei "
        "JOIN evaluations e ON e.id = ei.evaluation_id "
        "WHERE e.checklist_id = 9 AND ei.comment LIKE '%&quot;%'"
    )).fetchall()
    fixed = 0
    for item_id, comment in items:
        new_comment = _html.unescape(comment)
        if new_comment != comment:
            db.execute(text("UPDATE evaluation_items SET comment=:c WHERE id=:i"),
                       {"c": new_comment, "i": item_id})
            fixed += 1
    db.commit()
    return {"fixed": fixed}


@router.post("/admin/run-import")
def run_import(request: Request):
    token = request.headers.get("X-Deploy-Token", "")
    if token != DEPLOY_SECRET:
        from fastapi import HTTPException
        raise HTTPException(status_code=403)

    import sys
    result = subprocess.run(
        [sys.executable, "-m", "scripts.import_lena"],
        capture_output=True, text=True,
        cwd=os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    )
    return {
        "status": "ok" if result.returncode == 0 else "error",
        "stdout": result.stdout,
        "stderr": result.stderr,
    }
