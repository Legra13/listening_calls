import json
from datetime import datetime
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, joinedload, selectinload
from app.database import get_db
from app.models import (
    Block, Checklist, Criterion, Evaluation, User,
    CalibrationSession, CalibrationSessionEval, CalibrationParticipant,
    CalibrationParticipantEval, CalibrationAnswerItem, CalibrationItemResolution,
)
from app.deps import get_current_user, flash, pop_flash
from app.scoring import calculate_scores, score_color

router = APIRouter(prefix="/calibration")
templates = Jinja2Templates(directory="app/templates")


def _comment_text(raw: str | None) -> str | None:
    """Извлекает читаемый текст из комментария (JSON-массив или plain text)."""
    if not raw:
        return None
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            parts = [str(c.get("text", "")).strip() for c in data if isinstance(c, dict)]
            parts = [p for p in parts if p]
            return " | ".join(parts) if parts else None
    except (json.JSONDecodeError, ValueError):
        pass
    return raw.strip() or None


def _compute_comparison(source_eval: Evaluation, answers: list, checklist: Checklist):
    """Сравнивает оценку источника и ответы участника (по одной сделке)."""
    source_map = {item.criterion_id: item for item in source_eval.items}
    calib_map = {a.criterion_id: a for a in answers}

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
                "source_comment": _comment_text(sv.comment if sv else None),
                "calib_value": cv_val,
                "calib_comment": _comment_text(cv.comment if cv else None),
                "match": sv_val == cv_val,
                "both_na": both_na,
            })
        b_compared = sum(1 for c in criteria_data if not c["both_na"])
        b_match    = sum(1 for c in criteria_data if c["match"] and not c["both_na"])
        b_pct = round(b_match / b_compared * 100, 1) if b_compared > 0 else None
        blocks_data.append({
            "block": block,
            "criteria": criteria_data,
            "block_match_pct": b_pct,
            "block_compared": b_compared,
            "block_mismatch": b_compared - b_match if b_compared > 0 else 0,
        })

    match_pct = round(total_match / total_compared * 100, 1) if total_compared > 0 else 100.0
    mismatch_count = sum(
        1 for b in blocks_data for c in b["criteria"]
        if not c["match"] and not c["both_na"]
    )
    return blocks_data, match_pct, mismatch_count


def _match_counts(source_eval: Evaluation, answers: list, checklist: Checklist) -> tuple[int, int]:
    """Возвращает (сравнено, совпало) по одной сделке — для сводного %."""
    source_map = {item.criterion_id: item for item in source_eval.items}
    calib_map = {a.criterion_id: a for a in answers}
    compared = matched = 0
    for block in checklist.blocks:
        for crit in block.criteria:
            sv = source_map.get(crit.id)
            cv = calib_map.get(crit.id)
            sv_val = sv.value if sv else "na"
            cv_val = cv.value if cv else "na"
            if sv_val == "na" and cv_val == "na":
                continue
            compared += 1
            if sv_val == cv_val:
                matched += 1
    return compared, matched


def _answers_by_eval(participant: CalibrationParticipant) -> dict[int | None, list]:
    """Группирует ответы участника по session_eval_id."""
    grouped: dict[int | None, list] = {}
    for a in participant.answers:
        grouped.setdefault(a.session_eval_id, []).append(a)
    return grouped


def _session_match_pcts(sess: CalibrationSession):
    """% совпадения участников с исходными оценками по сессии.

    Требует полностью загруженную сессию (checklist.blocks.criteria,
    session_evals.source_evaluation.items, participants.answers).
    Возвращает:
      pe_match:   {(participant_id, session_eval_id): pct|None} — по каждой сделке
      part_match: {participant_id: pct|None} — общий по участнику (все его сделки)
      session_avg: float|None — средний % по участникам (итог сессии)
    """
    checklist = sess.checklist
    pe_match: dict = {}
    part_match: dict = {}
    if not checklist:
        return pe_match, part_match, None
    se_source = {se.id: se.source_evaluation for se in sess.session_evals}
    per_part_pcts = []
    for p in sess.participants:
        answers_by_eval = _answers_by_eval(p)
        p_compared = p_matched = 0
        for se in sess.session_evals:
            ans = answers_by_eval.get(se.id, [])
            src = se_source.get(se.id)
            if not ans or not src:
                continue
            c, m = _match_counts(src, ans, checklist)
            pe_match[(p.id, se.id)] = round(m / c * 100, 1) if c > 0 else None
            p_compared += c
            p_matched += m
        pct = round(p_matched / p_compared * 100, 1) if p_compared > 0 else None
        part_match[p.id] = pct
        if pct is not None:
            per_part_pcts.append(pct)
    session_avg = round(sum(per_part_pcts) / len(per_part_pcts), 1) if per_part_pcts else None
    return pe_match, part_match, session_avg


def _participant_own_evals(
    db: Session, participant: CalibrationParticipant,
    source_eval: Evaluation, checklist_id: int | None,
) -> list[Evaluation]:
    """Собственные (уже созданные) опубликованные оценки участника по той же сделке и чек-листу.

    Позволяет участнику подставить свою готовую оценку в форму калибровки вместо
    заполнения заново. Исходную оценку сессии исключаем. Свежие — первыми.
    """
    if not source_eval or not source_eval.deal_id:
        return []
    q = (
        db.query(Evaluation)
        .options(joinedload(Evaluation.items))
        .filter(
            Evaluation.evaluator_id == participant.user_id,
            Evaluation.deal_id == source_eval.deal_id,
            Evaluation.status == "published",
            Evaluation.id != source_eval.id,
        )
    )
    if checklist_id:
        q = q.filter(Evaluation.checklist_id == checklist_id)
    return q.order_by(Evaluation.created_at.desc()).all()


def _load_session(db: Session, session_id: int) -> CalibrationSession | None:
    return (
        db.query(CalibrationSession)
        .options(
            joinedload(CalibrationSession.checklist)
                .selectinload(Checklist.blocks).selectinload(Block.criteria),
            selectinload(CalibrationSession.session_evals).options(
                joinedload(CalibrationSessionEval.source_evaluation).selectinload(Evaluation.items),
                joinedload(CalibrationSessionEval.source_evaluation).joinedload(Evaluation.evaluator),
            ),
            joinedload(CalibrationSession.created_by),
            selectinload(CalibrationSession.participants).options(
                joinedload(CalibrationParticipant.user),
                selectinload(CalibrationParticipant.answers),
                selectinload(CalibrationParticipant.participant_evals),
            ),
            selectinload(CalibrationSession.resolutions),
        )
        .filter(CalibrationSession.id == session_id)
        .first()
    )


# ── List ──────────────────────────────────────────────────────────────────────

@router.get("")
def calibration_index(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    marked_evals = (
        db.query(Evaluation)
        .filter(Evaluation.is_calibration == True, Evaluation.status == "published")
        .options(
            joinedload(Evaluation.checklist),
            joinedload(Evaluation.evaluator),
        )
        .order_by(Evaluation.id.desc())
        .all()
    )

    sessions = (
        db.query(CalibrationSession)
        .options(
            joinedload(CalibrationSession.created_by),
            joinedload(CalibrationSession.checklist)
                .selectinload(Checklist.blocks).selectinload(Block.criteria),
            selectinload(CalibrationSession.session_evals)
                .joinedload(CalibrationSessionEval.source_evaluation).selectinload(Evaluation.items),
            selectinload(CalibrationSession.participants).joinedload(CalibrationParticipant.user),
            selectinload(CalibrationSession.participants).selectinload(CalibrationParticipant.answers),
        )
        .order_by(CalibrationSession.id.desc())
        .all()
    )
    # Карта eval_id → сессии, в которых сделка участвует
    sessions_by_eval: dict[int, list] = {}
    for s in sessions:
        for se in s.session_evals:
            sessions_by_eval.setdefault(se.source_evaluation_id, []).append(s)

    # Сводный % совпадения по каждой сессии (среднее по участникам)
    session_match: dict[int, float | None] = {}
    for s in sessions:
        _, _, avg = _session_match_pcts(s)
        session_match[s.id] = avg

    return templates.TemplateResponse("calibration/index.html", {
        "request": request,
        "current_user": current_user,
        "marked_evals": marked_evals,
        "sessions_by_eval": sessions_by_eval,
        "all_sessions": sessions,
        "session_match": session_match,
        "score_color": score_color,
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
        .limit(300)
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
    source_eval_ids = form.getlist("source_evaluation_ids")
    participant_ids = form.getlist("participant_ids")

    if not name or not source_eval_ids:
        flash(request, "Укажите название и хотя бы одну сделку", "danger")
        return RedirectResponse("/calibration/new", status_code=302)

    # Разбор и загрузка выбранных оценок
    eval_ids: list[int] = []
    for s in source_eval_ids:
        try:
            eval_ids.append(int(s))
        except ValueError:
            pass
    eval_ids = list(dict.fromkeys(eval_ids))  # уникальные, с сохранением порядка

    evals = db.query(Evaluation).filter(Evaluation.id.in_(eval_ids)).all()
    evals_by_id = {e.id: e for e in evals}
    ordered_evals = [evals_by_id[i] for i in eval_ids if i in evals_by_id]
    if not ordered_evals:
        flash(request, "Выбранные оценки не найдены", "danger")
        return RedirectResponse("/calibration/new", status_code=302)

    # Все сделки сессии должны быть по одному чек-листу
    checklist_ids = {e.checklist_id for e in ordered_evals}
    if len(checklist_ids) > 1:
        flash(request, "Все сделки сессии должны быть по одному чек-листу", "danger")
        return RedirectResponse("/calibration/new", status_code=302)
    checklist_id = ordered_evals[0].checklist_id

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
        source_evaluation_id=ordered_evals[0].id,
        checklist_id=checklist_id,
        created_by_id=current_user.id,
        status="open",
    )
    db.add(sess)
    db.flush()

    for idx, ev in enumerate(ordered_evals):
        db.add(CalibrationSessionEval(
            session_id=sess.id,
            source_evaluation_id=ev.id,
            order_index=idx,
        ))

    for uid_str in participant_ids:
        try:
            uid = int(uid_str)
            db.add(CalibrationParticipant(session_id=sess.id, user_id=uid))
        except ValueError:
            pass

    db.commit()
    flash(request, f"Сессия «{name}» создана ({len(ordered_evals)} сделок)")
    return RedirectResponse(f"/calibration/{sess.id}", status_code=302)


# ── View ──────────────────────────────────────────────────────────────────────

@router.get("/{session_id}")
def calibration_view(
    session_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sess = _load_session(db, session_id)
    if not sess:
        flash(request, "Сессия не найдена", "danger")
        return RedirectResponse("/calibration", status_code=302)

    users = (
        db.query(User)
        .filter(User.is_active == True)
        .order_by(User.full_name, User.username)
        .all()
    )
    existing_user_ids = {p.user_id for p in sess.participants}

    # Оценки-источники этого чек-листа, ещё не добавленные в сессию (для добавления сделки)
    in_session_eval_ids = {se.source_evaluation_id for se in sess.session_evals}
    available_evals = []
    if sess.checklist_id:
        available_evals = (
            db.query(Evaluation)
            .filter(
                Evaluation.is_calibration == True,
                Evaluation.status == "published",
                Evaluation.checklist_id == sess.checklist_id,
            )
            .order_by(Evaluation.id.desc())
            .limit(300)
            .all()
        )
        available_evals = [e for e in available_evals if e.id not in in_session_eval_ids]

    # Матрица: (participant_id, session_eval_id) → participant_eval
    pe_map = {}
    for p in sess.participants:
        for pe in p.participant_evals:
            pe_map[(p.id, pe.session_eval_id)] = pe

    # Прогресс участника в целом
    total_deals = len(sess.session_evals)
    participant_progress = {}
    for p in sess.participants:
        done = sum(
            1 for se in sess.session_evals
            if pe_map.get((p.id, se.id)) and pe_map[(p.id, se.id)].status == "completed"
        )
        participant_progress[p.id] = {"done": done, "total": total_deals}

    any_completed = any(
        pe.status == "completed" for p in sess.participants for pe in p.participant_evals
    )

    # % совпадения с исходными оценками: по сделке и общий по участнику
    pe_match, part_match, session_avg = _session_match_pcts(sess)

    return templates.TemplateResponse("calibration/view.html", {
        "request": request,
        "current_user": current_user,
        "sess": sess,
        "users": users,
        "existing_user_ids": existing_user_ids,
        "available_evals": available_evals,
        "pe_map": pe_map,
        "participant_progress": participant_progress,
        "any_completed": any_completed,
        "pe_match": pe_match,
        "part_match": part_match,
        "session_avg": session_avg,
        "score_color": score_color,
        "flash": pop_flash(request),
    })


# ── Participants ────────────────────────────────────────────────────────────────

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


# ── Deals (session evals) ───────────────────────────────────────────────────────

@router.post("/{session_id}/add-deal")
async def calibration_add_deal(
    session_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    form = await request.form()
    eval_id_str = (form.get("evaluation_id") or "").strip()
    sess = db.query(CalibrationSession).filter(CalibrationSession.id == session_id).first()
    if not sess or sess.status == "closed":
        flash(request, "Нельзя изменить закрытую сессию", "warning")
        return RedirectResponse(f"/calibration/{session_id}", status_code=302)
    try:
        eval_id = int(eval_id_str)
    except ValueError:
        flash(request, "Неверная сделка", "danger")
        return RedirectResponse(f"/calibration/{session_id}", status_code=302)

    ev = db.query(Evaluation).filter(Evaluation.id == eval_id).first()
    if not ev:
        flash(request, "Оценка не найдена", "danger")
        return RedirectResponse(f"/calibration/{session_id}", status_code=302)
    if sess.checklist_id and ev.checklist_id != sess.checklist_id:
        flash(request, "Сделка должна быть по тому же чек-листу, что и сессия", "danger")
        return RedirectResponse(f"/calibration/{session_id}", status_code=302)

    existing = db.query(CalibrationSessionEval).filter(
        CalibrationSessionEval.session_id == session_id,
        CalibrationSessionEval.source_evaluation_id == eval_id,
    ).first()
    if existing:
        flash(request, "Эта сделка уже в сессии", "warning")
        return RedirectResponse(f"/calibration/{session_id}", status_code=302)

    max_order = db.query(CalibrationSessionEval).filter(
        CalibrationSessionEval.session_id == session_id
    ).count()
    db.add(CalibrationSessionEval(
        session_id=session_id,
        source_evaluation_id=eval_id,
        order_index=max_order,
    ))
    db.commit()
    flash(request, "Сделка добавлена в сессию")
    return RedirectResponse(f"/calibration/{session_id}", status_code=302)


@router.post("/{session_id}/remove-deal/{session_eval_id}")
def calibration_remove_deal(
    session_id: int,
    session_eval_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sess = db.query(CalibrationSession).filter(CalibrationSession.id == session_id).first()
    if not sess or sess.status == "closed":
        flash(request, "Нельзя изменить закрытую сессию", "warning")
        return RedirectResponse(f"/calibration/{session_id}", status_code=302)

    se = db.query(CalibrationSessionEval).filter(
        CalibrationSessionEval.id == session_eval_id,
        CalibrationSessionEval.session_id == session_id,
    ).first()
    if not se:
        return RedirectResponse(f"/calibration/{session_id}", status_code=302)

    total = db.query(CalibrationSessionEval).filter(
        CalibrationSessionEval.session_id == session_id
    ).count()
    if total <= 1:
        flash(request, "Нельзя удалить единственную сделку сессии", "warning")
        return RedirectResponse(f"/calibration/{session_id}", status_code=302)

    removed_source_eval_id = se.source_evaluation_id

    # Явно чистим связанные ответы и итоги по этой сделке (answers каскадятся по participant)
    db.query(CalibrationAnswerItem).filter(
        CalibrationAnswerItem.session_eval_id == session_eval_id
    ).delete(synchronize_session=False)
    db.query(CalibrationItemResolution).filter(
        CalibrationItemResolution.session_eval_id == session_eval_id
    ).delete(synchronize_session=False)
    # participant_evals каскадятся по session_eval
    db.delete(se)

    # Если удалили первичную сделку — переназначаем source_evaluation_id
    if sess.source_evaluation_id == removed_source_eval_id:
        remaining = db.query(CalibrationSessionEval).filter(
            CalibrationSessionEval.session_id == session_id,
            CalibrationSessionEval.id != session_eval_id,
        ).order_by(CalibrationSessionEval.order_index).first()
        if remaining:
            sess.source_evaluation_id = remaining.source_evaluation_id

    db.commit()
    flash(request, "Сделка удалена из сессии")
    return RedirectResponse(f"/calibration/{session_id}", status_code=302)


# ── Evaluate ──────────────────────────────────────────────────────────────────

@router.get("/{session_id}/evaluate/{participant_id}/{session_eval_id}")
def calibration_evaluate_form(
    session_id: int,
    participant_id: int,
    session_eval_id: int,
    request: Request,
    prefill: int | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sess = _load_session(db, session_id)
    if not sess:
        flash(request, "Сессия не найдена", "danger")
        return RedirectResponse("/calibration", status_code=302)

    participant = next((p for p in sess.participants if p.id == participant_id), None)
    session_eval = next((se for se in sess.session_evals if se.id == session_eval_id), None)
    if not participant or not session_eval:
        flash(request, "Участник или сделка не найдены", "danger")
        return RedirectResponse(f"/calibration/{session_id}", status_code=302)

    answers_by_eval = _answers_by_eval(participant)
    answer_map = {a.criterion_id: a for a in answers_by_eval.get(session_eval_id, [])}

    pe = next((x for x in participant.participant_evals if x.session_eval_id == session_eval_id), None)
    gen_comment = pe.general_comment if pe else None

    # Собственные готовые оценки участника по этой сделке — можно подставить в форму
    own_evals = _participant_own_evals(db, participant, session_eval.source_evaluation, sess.checklist_id)
    prefilled_from = None
    if prefill:
        src = next((e for e in own_evals if e.id == prefill), None)
        if src:
            # Подставляем ответы (и общий комментарий) из готовой оценки —
            # ничего не сохраняем, пользователь проверит и нажмёт «Сохранить».
            # EvaluationItem дуально совместим с шаблоном (.value / .comment).
            answer_map = {it.criterion_id: it for it in src.items}
            if src.general_comment:
                gen_comment = src.general_comment
            prefilled_from = src

    # Навигация по сделкам
    ordered = sess.session_evals
    idx = next((i for i, se in enumerate(ordered) if se.id == session_eval_id), 0)
    prev_se = ordered[idx - 1] if idx > 0 else None
    next_se = ordered[idx + 1] if idx < len(ordered) - 1 else None

    return templates.TemplateResponse("calibration/evaluate.html", {
        "request": request,
        "current_user": current_user,
        "sess": sess,
        "participant": participant,
        "session_eval": session_eval,
        "source_eval": session_eval.source_evaluation,
        "checklist": sess.checklist,
        "answer_map": answer_map,
        "gen_comment": gen_comment,
        "own_evals": own_evals,
        "prefilled_from": prefilled_from,
        "deal_index": idx + 1,
        "deal_total": len(ordered),
        "prev_se": prev_se,
        "next_se": next_se,
        "flash": pop_flash(request),
    })


@router.post("/{session_id}/evaluate/{participant_id}/{session_eval_id}")
async def calibration_evaluate_save(
    session_id: int,
    participant_id: int,
    session_eval_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sess = _load_session(db, session_id)
    if not sess:
        return RedirectResponse("/calibration", status_code=302)

    participant = next((p for p in sess.participants if p.id == participant_id), None)
    session_eval = next((se for se in sess.session_evals if se.id == session_eval_id), None)
    if not participant or not session_eval:
        return RedirectResponse(f"/calibration/{session_id}", status_code=302)

    form = await request.form()
    checklist = sess.checklist
    all_criteria = [c for block in checklist.blocks for c in block.criteria]

    # Удаляем прежние ответы участника по ЭТОЙ сделке
    db.query(CalibrationAnswerItem).filter(
        CalibrationAnswerItem.participant_id == participant_id,
        CalibrationAnswerItem.session_eval_id == session_eval_id,
    ).delete(synchronize_session=False)

    items_raw = []
    for crit in all_criteria:
        value = str(form.get(f"criterion_{crit.id}", "na"))
        if value not in ("yes", "no", "na"):
            value = "na"
        comment = (form.get(f"comment_{crit.id}") or "").strip() or None
        db.add(CalibrationAnswerItem(
            participant_id=participant_id,
            session_eval_id=session_eval_id,
            criterion_id=crit.id,
            value=value,
            comment=comment,
        ))
        items_raw.append((crit.id, value, comment))

    total_score, _ = calculate_scores(items_raw, checklist)

    # Upsert прогресса участника по сделке
    pe = db.query(CalibrationParticipantEval).filter(
        CalibrationParticipantEval.participant_id == participant_id,
        CalibrationParticipantEval.session_eval_id == session_eval_id,
    ).first()
    if not pe:
        pe = CalibrationParticipantEval(
            participant_id=participant_id,
            session_eval_id=session_eval_id,
        )
        db.add(pe)
    pe.total_score = total_score
    pe.general_comment = (form.get("general_comment") or "").strip() or None
    pe.status = "completed"
    pe.completed_at = datetime.utcnow()
    db.flush()  # autoflush выключен — фиксируем pe, чтобы попал в подсчёт ниже

    # Общий статус участника: completed, если заполнены все сделки
    completed_evals = db.query(CalibrationParticipantEval).filter(
        CalibrationParticipantEval.participant_id == participant_id,
        CalibrationParticipantEval.status == "completed",
    ).count()
    total_deals = len(sess.session_evals)
    participant.status = "completed" if completed_evals >= total_deals else "pending"
    if participant.status == "completed":
        participant.completed_at = datetime.utcnow()

    db.commit()

    # Переход к следующей незаполненной сделке или назад к сессии
    ordered = sess.session_evals
    idx = next((i for i, se in enumerate(ordered) if se.id == session_eval_id), 0)
    if idx < len(ordered) - 1:
        nxt = ordered[idx + 1]
        flash(request, f"Сохранено ({total_score:.1f}%). Сделка {idx + 2} из {len(ordered)}.")
        return RedirectResponse(
            f"/calibration/{session_id}/evaluate/{participant_id}/{nxt.id}", status_code=302
        )
    flash(request, f"Оценка сохранена. Итог: {total_score:.1f}%")
    return RedirectResponse(f"/calibration/{session_id}", status_code=302)


# ── Compare ───────────────────────────────────────────────────────────────────

@router.get("/{session_id}/compare")
def calibration_compare(
    session_id: int,
    request: Request,
    pid: int | None = None,
    se: int | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sess = _load_session(db, session_id)
    if not sess:
        flash(request, "Сессия не найдена", "danger")
        return RedirectResponse("/calibration", status_code=302)

    checklist = sess.checklist
    ordered_evals = sess.session_evals

    # Есть ли хоть один заполненный ответ
    any_completed = any(
        pe.status == "completed" for p in sess.participants for pe in p.participant_evals
    )
    if not any_completed:
        flash(request, "Нет завершённых оценок для сравнения", "warning")
        return RedirectResponse(f"/calibration/{session_id}", status_code=302)

    # Сводка: по каждому участнику — общий % совпадения по всем сделкам
    summary = []
    for p in sess.participants:
        answers_by_eval = _answers_by_eval(p)
        total_compared = total_matched = 0
        deals_done = 0
        for se_obj in ordered_evals:
            ans = answers_by_eval.get(se_obj.id, [])
            if not ans:
                continue
            deals_done += 1
            c, m = _match_counts(se_obj.source_evaluation, ans, checklist)
            total_compared += c
            total_matched += m
        pct = round(total_matched / total_compared * 100, 1) if total_compared > 0 else None
        summary.append({
            "participant": p,
            "match_pct": pct,
            "deals_done": deals_done,
            "deals_total": len(ordered_evals),
        })

    # Детализация по выбранной сделке + участнику
    active_se = next((x for x in ordered_evals if x.id == se), None) or (ordered_evals[0] if ordered_evals else None)
    # Участники, заполнившие выбранную сделку
    def _did(p, se_id):
        return any(a.session_eval_id == se_id for a in p.answers)
    participants_for_se = [p for p in sess.participants if active_se and _did(p, active_se.id)]

    active_participant = None
    if participants_for_se:
        active_participant = next((p for p in participants_for_se if p.id == pid), participants_for_se[0])

    blocks_data, match_pct, mismatch_count = [], None, 0
    if active_participant and active_se:
        ans = [a for a in active_participant.answers if a.session_eval_id == active_se.id]
        blocks_data, match_pct, mismatch_count = _compute_comparison(
            active_se.source_evaluation, ans, checklist
        )

    # Итоги (resolutions) по выбранной сделке
    resolution_map = {
        r.criterion_id: r for r in sess.resolutions
        if active_se and r.session_eval_id == active_se.id
    }

    return templates.TemplateResponse("calibration/compare.html", {
        "request": request,
        "current_user": current_user,
        "sess": sess,
        "ordered_evals": ordered_evals,
        "summary": summary,
        "active_se": active_se,
        "participants_for_se": participants_for_se,
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
    sess = db.query(CalibrationSession).options(
        joinedload(CalibrationSession.resolutions)
    ).filter(CalibrationSession.id == session_id).first()
    if not sess:
        return RedirectResponse("/calibration", status_code=302)

    form = await request.form()
    try:
        se_id = int((form.get("session_eval_id") or "").strip())
    except ValueError:
        se_id = None

    existing = {
        r.criterion_id: r for r in sess.resolutions
        if r.session_eval_id == se_id
    }

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
                    session_eval_id=se_id,
                    criterion_id=crit_id,
                    final_value=final_value,
                    comment=comment,
                    resolved_by_id=current_user.id,
                    resolved_at=datetime.utcnow(),
                ))

    db.commit()
    flash(request, "Итоги сохранены")

    pid = (form.get("active_pid") or "").strip()
    redirect_url = f"/calibration/{session_id}/compare"
    params = []
    if pid:
        params.append(f"pid={pid}")
    if se_id:
        params.append(f"se={se_id}")
    if params:
        redirect_url += "?" + "&".join(params)
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
