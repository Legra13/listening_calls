from datetime import datetime
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, joinedload
from app.database import get_db
from app.models import (
    Block, Checklist, Criterion, Evaluation, User,
    CalibrationSession, CalibrationParticipant,
    CalibrationAnswerItem, CalibrationItemResolution,
)
from app.deps import get_current_user, flash, pop_flash
from app.scoring import calculate_scores, score_color

router = APIRouter(prefix="/calibration")
templates = Jinja2Templates(directory="app/templates")


def _compute_comparison(source_eval: Evaluation, participant: CalibrationParticipant, checklist: Checklist):
    """Сравнивает оценку источника и ответы участника калибровки."""
    source_map = {item.criterion_id: item for item in source_eval.items}
    calib_map = {a.criterion_id: a for a in participant.answers}

    blocks_data = []
    total_compared = 0
    total_match = 0

    for block in checklist.blocks:
        criteria_data = []
        for crit in block.criteria:
            sv = source_map.get(crit.id)
            cv = calib_map.get(crit.id)
            sv_val = sv.value if sv else "na"
            cv_val = cv.value if cv else "na"

            both_na = sv_val == "na" and cv_val == "na"
            if not both_na:
                total_compared += 1
                if sv_val == cv_val:
                    total_match += 1

            criteria_data.append({
                "criterion": crit,
                "source_value": sv_val,
                "source_comment": sv.comment if sv else None,
                "calib_value": cv_val,
                "calib_comment": cv.comment if cv else None,
                "match": sv_val == cv_val,
                "both_na": both_na,
            })
        blocks_data.append({"block": block, "criteria": criteria_data})

    match_pct = round(total_match / total_compared * 100, 1) if total_compared > 0 else 100.0
    mismatch_count = sum(
        1 for b in blocks_data for c in b["criteria"]
        if not c["match"] and not c["both_na"]
    )
    return blocks_data, match_pct, mismatch_count


# ── List ──────────────────────────────────────────────────────────────────────

@router.get("")
def calibration_index(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sessions = (
        db.query(CalibrationSession)
        .options(
            joinedload(CalibrationSession.source_evaluation).joinedload(Evaluation.checklist),
            joinedload(CalibrationSession.created_by),
            joinedload(CalibrationSession.participants).joinedload(CalibrationParticipant.user),
        )
        .order_by(CalibrationSession.id.desc())
        .all()
    )
    return templates.TemplateResponse("calibration/index.html", {
        "request": request,
        "current_user": current_user,
        "sessions": sessions,
        "flash": pop_flash(request),
    })


# ── New / Create ──────────────────────────────────────────────────────────────

@router.get("/new")
def calibration_new(
    request: Request,
    eval_id: int | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    calibration_evals = (
        db.query(Evaluation)
        .filter(Evaluation.is_calibration == True, Evaluation.status == "published")
        .options(joinedload(Evaluation.checklist))
        .order_by(Evaluation.id.desc())
        .limit(200)
        .all()
    )
    users = (
        db.query(User)
        .filter(User.is_active == True)
        .order_by(User.full_name, User.username)
        .all()
    )
    preselected_eval = None
    if eval_id:
        preselected_eval = db.query(Evaluation).filter(Evaluation.id == eval_id).first()
    return templates.TemplateResponse("calibration/new.html", {
        "request": request,
        "current_user": current_user,
        "calibration_evals": calibration_evals,
        "users": users,
        "preselected_eval": preselected_eval,
        "flash": pop_flash(request),
    })


@router.post("")
async def calibration_create(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    form = await request.form()
    name = (form.get("name") or "").strip()
    description = (form.get("description") or "").strip() or None
    session_date_str = (form.get("session_date") or "").strip()
    source_eval_id_str = (form.get("source_evaluation_id") or "").strip()
    participant_ids = form.getlist("participant_ids")

    if not name or not source_eval_id_str:
        flash(request, "Укажите название и исходную оценку", "danger")
        return RedirectResponse("/calibration/new", status_code=302)

    try:
        source_eval_id = int(source_eval_id_str)
    except ValueError:
        flash(request, "Неверный ID оценки", "danger")
        return RedirectResponse("/calibration/new", status_code=302)

    source_eval = db.query(Evaluation).filter(Evaluation.id == source_eval_id).first()
    if not source_eval:
        flash(request, "Оценка не найдена", "danger")
        return RedirectResponse("/calibration/new", status_code=302)

    session_date = None
    if session_date_str:
        try:
            session_date = datetime.strptime(session_date_str, "%Y-%m-%d")
        except ValueError:
            pass

    sess = CalibrationSession(
        name=name,
        description=description,
        session_date=session_date,
        source_evaluation_id=source_eval_id,
        created_by_id=current_user.id,
        status="open",
    )
    db.add(sess)
    db.flush()

    for uid_str in participant_ids:
        try:
            uid = int(uid_str)
            db.add(CalibrationParticipant(session_id=sess.id, user_id=uid))
        except ValueError:
            pass

    db.commit()
    flash(request, f"Сессия «{name}» создана")
    return RedirectResponse(f"/calibration/{sess.id}", status_code=302)


# ── View ──────────────────────────────────────────────────────────────────────

@router.get("/{session_id}")
def calibration_view(
    session_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sess = (
        db.query(CalibrationSession)
        .options(
            joinedload(CalibrationSession.source_evaluation).joinedload(Evaluation.checklist),
            joinedload(CalibrationSession.source_evaluation).joinedload(Evaluation.evaluator),
            joinedload(CalibrationSession.created_by),
            joinedload(CalibrationSession.participants).joinedload(CalibrationParticipant.user),
        )
        .filter(CalibrationSession.id == session_id)
        .first()
    )
    if not sess:
        flash(request, "Сессия не найдена", "danger")
        return RedirectResponse("/calibration", status_code=302)

    users = (
        db.query(User)
        .filter(User.is_active == True)
        .order_by(User.full_name, User.username)
        .all()
    )
    # Exclude already-added participants
    existing_user_ids = {p.user_id for p in sess.participants}

    return templates.TemplateResponse("calibration/view.html", {
        "request": request,
        "current_user": current_user,
        "sess": sess,
        "users": users,
        "existing_user_ids": existing_user_ids,
        "score_color": score_color,
        "flash": pop_flash(request),
    })


# ── Add participant ────────────────────────────────────────────────────────────

@router.post("/{session_id}/add-participant")
async def calibration_add_participant(
    session_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    form = await request.form()
    uid_str = (form.get("user_id") or "").strip()
    sess = db.query(CalibrationSession).filter(CalibrationSession.id == session_id).first()
    if not sess or sess.status == "closed":
        flash(request, "Нельзя изменить закрытую сессию", "warning")
        return RedirectResponse(f"/calibration/{session_id}", status_code=302)
    try:
        uid = int(uid_str)
        existing = db.query(CalibrationParticipant).filter(
            CalibrationParticipant.session_id == session_id,
            CalibrationParticipant.user_id == uid,
        ).first()
        if not existing:
            db.add(CalibrationParticipant(session_id=session_id, user_id=uid))
            db.commit()
            flash(request, "Участник добавлен")
        else:
            flash(request, "Участник уже добавлен", "warning")
    except ValueError:
        flash(request, "Неверный пользователь", "danger")
    return RedirectResponse(f"/calibration/{session_id}", status_code=302)


# ── Remove participant ─────────────────────────────────────────────────────────

@router.post("/{session_id}/remove-participant/{participant_id}")
def calibration_remove_participant(
    session_id: int,
    participant_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sess = db.query(CalibrationSession).filter(CalibrationSession.id == session_id).first()
    if sess and sess.status == "closed":
        flash(request, "Нельзя изменить закрытую сессию", "warning")
        return RedirectResponse(f"/calibration/{session_id}", status_code=302)
    p = db.query(CalibrationParticipant).filter(
        CalibrationParticipant.id == participant_id,
        CalibrationParticipant.session_id == session_id,
    ).first()
    if p:
        db.delete(p)
        db.commit()
        flash(request, "Участник удалён")
    return RedirectResponse(f"/calibration/{session_id}", status_code=302)


# ── Evaluate ──────────────────────────────────────────────────────────────────

@router.get("/{session_id}/evaluate/{participant_id}")
def calibration_evaluate_form(
    session_id: int,
    participant_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    participant = (
        db.query(CalibrationParticipant)
        .options(joinedload(CalibrationParticipant.answers))
        .filter(
            CalibrationParticipant.id == participant_id,
            CalibrationParticipant.session_id == session_id,
        )
        .first()
    )
    if not participant:
        flash(request, "Участник не найден", "danger")
        return RedirectResponse(f"/calibration/{session_id}", status_code=302)

    sess = (
        db.query(CalibrationSession)
        .options(
            joinedload(CalibrationSession.source_evaluation).options(
                joinedload(Evaluation.checklist).joinedload(Checklist.blocks).joinedload(Block.criteria),
            ),
        )
        .filter(CalibrationSession.id == session_id)
        .first()
    )

    answer_map = {a.criterion_id: a for a in participant.answers}

    return templates.TemplateResponse("calibration/evaluate.html", {
        "request": request,
        "current_user": current_user,
        "sess": sess,
        "participant": participant,
        "source_eval": sess.source_evaluation,
        "checklist": sess.source_evaluation.checklist,
        "answer_map": answer_map,
        "flash": pop_flash(request),
    })


@router.post("/{session_id}/evaluate/{participant_id}")
async def calibration_evaluate_save(
    session_id: int,
    participant_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    participant = (
        db.query(CalibrationParticipant)
        .filter(
            CalibrationParticipant.id == participant_id,
            CalibrationParticipant.session_id == session_id,
        )
        .first()
    )
    if not participant:
        return RedirectResponse(f"/calibration/{session_id}", status_code=302)

    sess = (
        db.query(CalibrationSession)
        .options(
            joinedload(CalibrationSession.source_evaluation).options(
                joinedload(Evaluation.checklist).joinedload(Checklist.blocks).joinedload(Block.criteria),
            ),
        )
        .filter(CalibrationSession.id == session_id)
        .first()
    )

    form = await request.form()
    checklist = sess.source_evaluation.checklist
    all_criteria = [c for block in checklist.blocks for c in block.criteria]

    db.query(CalibrationAnswerItem).filter(
        CalibrationAnswerItem.participant_id == participant_id
    ).delete()

    items_raw = []
    for crit in all_criteria:
        value = str(form.get(f"criterion_{crit.id}", "na"))
        if value not in ("yes", "no", "na"):
            value = "na"
        comment = (form.get(f"comment_{crit.id}") or "").strip() or None
        db.add(CalibrationAnswerItem(
            participant_id=participant_id,
            criterion_id=crit.id,
            value=value,
            comment=comment,
        ))
        items_raw.append((crit.id, value, comment))

    total_score, _ = calculate_scores(items_raw, checklist)
    participant.total_score = total_score
    participant.general_comment = (form.get("general_comment") or "").strip() or None
    participant.status = "completed"
    participant.completed_at = datetime.utcnow()

    db.commit()
    flash(request, f"Оценка сохранена. Итог: {total_score:.1f}%")
    return RedirectResponse(f"/calibration/{session_id}", status_code=302)


# ── Compare ───────────────────────────────────────────────────────────────────

@router.get("/{session_id}/compare")
def calibration_compare(
    session_id: int,
    request: Request,
    pid: int | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sess = (
        db.query(CalibrationSession)
        .options(
            joinedload(CalibrationSession.source_evaluation).options(
                joinedload(Evaluation.checklist).joinedload(Checklist.blocks).joinedload(Block.criteria),
                joinedload(Evaluation.items),
                joinedload(Evaluation.evaluator),
            ),
            joinedload(CalibrationSession.created_by),
            joinedload(CalibrationSession.participants).options(
                joinedload(CalibrationParticipant.user),
                joinedload(CalibrationParticipant.answers),
            ),
            joinedload(CalibrationSession.resolutions),
        )
        .filter(CalibrationSession.id == session_id)
        .first()
    )
    if not sess:
        flash(request, "Сессия не найдена", "danger")
        return RedirectResponse("/calibration", status_code=302)

    completed_participants = [p for p in sess.participants if p.status == "completed"]
    if not completed_participants:
        flash(request, "Нет завершённых оценок для сравнения", "warning")
        return RedirectResponse(f"/calibration/{session_id}", status_code=302)

    # Select participant to compare (default to first completed)
    active_pid = pid
    if active_pid is None:
        active_pid = completed_participants[0].id

    active_participant = next((p for p in completed_participants if p.id == active_pid), completed_participants[0])

    checklist = sess.source_evaluation.checklist
    blocks_data, match_pct, mismatch_count = _compute_comparison(
        sess.source_evaluation, active_participant, checklist
    )

    resolution_map = {r.criterion_id: r for r in sess.resolutions}

    return templates.TemplateResponse("calibration/compare.html", {
        "request": request,
        "current_user": current_user,
        "sess": sess,
        "completed_participants": completed_participants,
        "active_participant": active_participant,
        "blocks_data": blocks_data,
        "match_pct": match_pct,
        "mismatch_count": mismatch_count,
        "resolution_map": resolution_map,
        "score_color": score_color,
        "flash": pop_flash(request),
    })


# ── Resolve ───────────────────────────────────────────────────────────────────

@router.post("/{session_id}/resolve")
async def calibration_resolve(
    session_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sess = db.query(CalibrationSession).filter(CalibrationSession.id == session_id).first()
    if not sess:
        return RedirectResponse("/calibration", status_code=302)

    form = await request.form()

    # Read all criterion keys from form
    existing = {r.criterion_id: r for r in sess.resolutions}

    for key in form:
        if key.startswith("final_value_"):
            try:
                crit_id = int(key.split("_", 2)[2])
            except (ValueError, IndexError):
                continue
            final_value = (form.get(f"final_value_{crit_id}") or "").strip() or None
            comment = (form.get(f"resolution_comment_{crit_id}") or "").strip() or None

            if final_value not in ("yes", "no", "na", None):
                final_value = None

            if crit_id in existing:
                r = existing[crit_id]
                r.final_value = final_value
                r.comment = comment
                r.resolved_by_id = current_user.id
                r.resolved_at = datetime.utcnow()
            elif final_value or comment:
                db.add(CalibrationItemResolution(
                    session_id=session_id,
                    criterion_id=crit_id,
                    final_value=final_value,
                    comment=comment,
                    resolved_by_id=current_user.id,
                    resolved_at=datetime.utcnow(),
                ))

    db.commit()
    flash(request, "Итоги сохранены")

    # Redirect back to compare with same participant
    pid = (form.get("active_pid") or "").strip()
    redirect_url = f"/calibration/{session_id}/compare"
    if pid:
        redirect_url += f"?pid={pid}"
    return RedirectResponse(redirect_url, status_code=302)


# ── Close / Reopen ────────────────────────────────────────────────────────────

@router.post("/{session_id}/close")
def calibration_close(
    session_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sess = db.query(CalibrationSession).filter(CalibrationSession.id == session_id).first()
    if sess:
        sess.status = "closed"
        sess.updated_at = datetime.utcnow()
        db.commit()
        flash(request, "Сессия закрыта")
    return RedirectResponse(f"/calibration/{session_id}", status_code=302)


@router.post("/{session_id}/reopen")
def calibration_reopen(
    session_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sess = db.query(CalibrationSession).filter(CalibrationSession.id == session_id).first()
    if sess:
        sess.status = "open"
        sess.updated_at = datetime.utcnow()
        db.commit()
        flash(request, "Сессия открыта повторно")
    return RedirectResponse(f"/calibration/{session_id}", status_code=302)


# ── Delete ────────────────────────────────────────────────────────────────────

@router.post("/{session_id}/delete")
def calibration_delete(
    session_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sess = db.query(CalibrationSession).filter(CalibrationSession.id == session_id).first()
    if sess:
        db.delete(sess)
        db.commit()
        flash(request, "Сессия удалена")
    return RedirectResponse("/calibration", status_code=302)
