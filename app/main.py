from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from app.config import SECRET_KEY
from app.database import create_tables
from app.deps import NotAuthenticatedException
from app.routers import api, auth, users, checklists, evaluations, reports, export

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


def _run_migrations():
    from app.database import engine
    with engine.connect() as conn:
        cols = [r[1] for r in conn.execute(
            __import__("sqlalchemy").text("PRAGMA table_info(checklists)")
        ).fetchall()]
        if "departments" not in cols:
            conn.execute(__import__("sqlalchemy").text(
                "ALTER TABLE checklists ADD COLUMN departments VARCHAR(500)"
            ))
            conn.commit()
