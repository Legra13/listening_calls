"""
Внутреннее API для HTMX-запросов.
"""
from datetime import datetime
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import Block, Checklist, Criterion, DealCache, User
from app.deps import get_current_user
from app.bitrix import get_deal, DealInfo

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
    cached = db.query(DealCache).filter(DealCache.deal_id == deal_id).first()
    if cached:
        return _deal_response(
            deal_id=deal_id,
            operator_name=cached.operator_name or "",
            department=cached.department,
            deal_date=cached.deal_date,
            stage=cached.stage or "в работе",
            from_cache=True,
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


def _deal_response(
    deal_id: str,
    operator_name: str,
    department: str | None,
    deal_date: datetime | None,
    stage: str,
    from_cache: bool,
) -> JSONResponse:
    badge_class, badge_label = STAGE_BADGE.get(stage, ("secondary", stage))
    return JSONResponse({
        "deal_id": deal_id,
        "operator_name": operator_name,
        "department": department or "",
        "deal_date": deal_date.strftime("%Y-%m-%d") if deal_date else "",
        "stage": stage,
        "stage_badge": badge_class,
        "stage_label": badge_label,
        "from_cache": from_cache,
    })
