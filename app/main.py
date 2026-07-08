import threading
import time
import logging
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from app.config import SECRET_KEY
from app.database import create_tables
from app.deps import NotAuthenticatedException
from app.routers import api, auth, users, checklists, evaluations, reports, export, calibration

logger = logging.getLogger(__name__)

app = FastAPI(title="Оценка звонков", docs_url=None, redoc_url=None)
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, max_age=86400 * 30)
app.mount("/static", StaticFiles(directory="app/static"), name="static")

app.include_router(api.router)
app.include_router(auth.router)
app.include_router(users.router)
app.include_router(checklists.router)
app.include_router(evaluations.router)
app.include_router(reports.router)
app.include_router(export.router)
app.include_router(calibration.router)


@app.exception_handler(NotAuthenticatedException)
async def not_authenticated_handler(request: Request, exc: NotAuthenticatedException):
    return RedirectResponse(url="/login", status_code=302)


@app.get("/")
def root():
    return RedirectResponse("/evaluations", status_code=302)


@app.on_event("startup")
def startup():
    create_tables()
    _run_migrations()
    threading.Thread(target=_stage_sync_loop, daemon=True).start()


def _sync_deal_stages():
    """Обновляет стадии сделок в оценках со статусом 'в работе'."""
    from app.database import SessionLocal
    from app.models import Evaluation
    from app.bitrix import get_deal
    from datetime import datetime

    db = SessionLocal()
    try:
        rows = (
            db.query(Evaluation.id, Evaluation.deal_id)
            .filter(Evaluation.deal_id.isnot(None), Evaluation.stage == "в работе")
            .all()
        )
        updated = 0
        for ev_id, deal_id in rows:
            try:
                info = get_deal(deal_id)
                if info and info.stage != "в работе":
                    db.query(Evaluation).filter(Evaluation.id == ev_id).update(
                        {"stage": info.stage}
                    )
                    updated += 1
            except Exception:
                pass
        if updated:
            db.commit()
            logger.info("Stage sync: updated %d evaluations", updated)
    except Exception as e:
        logger.error("Stage sync error: %s", e)
    finally:
        db.close()


def _stage_sync_loop():
    # Первый запуск через минуту после старта, затем каждый час
    time.sleep(60)
    while True:
        _sync_deal_stages()
        time.sleep(3600)


def _run_migrations():
    from app.database import engine
    from sqlalchemy import text
    with engine.connect() as conn:
        def _cols(table):
            return [r[1] for r in conn.execute(text(f"PRAGMA table_info({table})")).fetchall()]

        cl_cols = _cols("checklists")
        if "departments" not in cl_cols:
            conn.execute(text("ALTER TABLE checklists ADD COLUMN departments VARCHAR(500)"))

        usr_cols = _cols("users")
        if "full_name" not in usr_cols:
            conn.execute(text("ALTER TABLE users ADD COLUMN full_name VARCHAR(200)"))

        ev_cols = _cols("evaluations")
        if "status" not in ev_cols:
            conn.execute(text("ALTER TABLE evaluations ADD COLUMN status VARCHAR(20) DEFAULT 'published'"))
        if "updated_at" not in ev_cols:
            conn.execute(text("ALTER TABLE evaluations ADD COLUMN updated_at DATETIME"))
        if "deal_url" not in ev_cols:
            conn.execute(text("ALTER TABLE evaluations ADD COLUMN deal_url VARCHAR(300)"))
        if "is_calibration" not in ev_cols:
            conn.execute(text("ALTER TABLE evaluations ADD COLUMN is_calibration BOOLEAN DEFAULT 0"))

        dc_cols = _cols("deal_cache")
        if "presentation_date" not in dc_cols:
            conn.execute(text("ALTER TABLE deal_cache ADD COLUMN presentation_date DATETIME"))

        # Калибровка по нескольким сделкам: новые колонки + бэкфилл существующих сессий.
        # Таблицы calibration_session_evals / calibration_participant_evals создаёт create_tables().
        cs_cols = _cols("calibration_sessions")
        if "checklist_id" not in cs_cols:
            conn.execute(text("ALTER TABLE calibration_sessions ADD COLUMN checklist_id INTEGER"))
        cai_cols = _cols("calibration_answer_items")
        if "session_eval_id" not in cai_cols:
            conn.execute(text("ALTER TABLE calibration_answer_items ADD COLUMN session_eval_id INTEGER"))
        cir_cols = _cols("calibration_item_resolutions")
        if "session_eval_id" not in cir_cols:
            conn.execute(text("ALTER TABLE calibration_item_resolutions ADD COLUMN session_eval_id INTEGER"))

        conn.commit()

        # Бэкфилл: одна сделка на сессию → одна запись в calibration_session_evals.
        # Идемпотентно: выполняется только если session_evals ещё пуст.
        se_count = conn.execute(text("SELECT COUNT(*) FROM calibration_session_evals")).scalar()
        if se_count == 0:
            conn.execute(text(
                "INSERT INTO calibration_session_evals (session_id, source_evaluation_id, order_index) "
                "SELECT id, source_evaluation_id, 0 FROM calibration_sessions"
            ))
            conn.execute(text(
                "UPDATE calibration_sessions SET checklist_id = "
                "(SELECT e.checklist_id FROM evaluations e WHERE e.id = calibration_sessions.source_evaluation_id) "
                "WHERE checklist_id IS NULL"
            ))
            # Ответы участников → session_eval своей сессии (у каждой сессии ровно одна сделка)
            conn.execute(text(
                "UPDATE calibration_answer_items SET session_eval_id = ("
                "  SELECT se.id FROM calibration_session_evals se "
                "  JOIN calibration_participants p ON p.session_id = se.session_id "
                "  WHERE p.id = calibration_answer_items.participant_id) "
                "WHERE session_eval_id IS NULL"
            ))
            conn.execute(text(
                "UPDATE calibration_item_resolutions SET session_eval_id = ("
                "  SELECT se.id FROM calibration_session_evals se "
                "  WHERE se.session_id = calibration_item_resolutions.session_id) "
                "WHERE session_eval_id IS NULL"
            ))
            # Прогресс участников → participant_evals
            conn.execute(text(
                "INSERT INTO calibration_participant_evals "
                "(participant_id, session_eval_id, total_score, general_comment, status, completed_at) "
                "SELECT p.id, se.id, p.total_score, p.general_comment, p.status, p.completed_at "
                "FROM calibration_participants p "
                "JOIN calibration_session_evals se ON se.session_id = p.session_id"
            ))
            conn.commit()
