from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from app.database import get_db
from app.auth import authenticate_user
from app.deps import flash, pop_flash

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/login")
def login_page(request: Request):
    if request.session.get("user_id"):
        return RedirectResponse("/evaluations", status_code=302)
    return templates.TemplateResponse("auth/login.html", {
        "request": request,
        "flash": pop_flash(request),
    })


@router.post("/login")
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = authenticate_user(db, username, password)
    if not user:
        flash(request, "Неверный логин или пароль", "danger")
        return RedirectResponse("/login", status_code=302)
    request.session["user_id"] = user.id
    return RedirectResponse("/evaluations", status_code=302)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=302)
