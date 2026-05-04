from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import User
from app.auth import create_user, hash_password
from app.deps import get_current_user, flash, pop_flash

router = APIRouter(prefix="/users")
templates = Jinja2Templates(directory="app/templates")


@router.get("")
def users_list(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    users = db.query(User).order_by(User.created_at).all()
    return templates.TemplateResponse("users/index.html", {
        "request": request,
        "current_user": current_user,
        "users": users,
        "flash": pop_flash(request),
    })


@router.post("/create")
def users_create(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    existing = db.query(User).filter(User.username == username).first()
    if existing:
        flash(request, f"Пользователь «{username}» уже существует", "danger")
        return RedirectResponse("/users", status_code=302)
    create_user(db, username, password)
    flash(request, f"Пользователь «{username}» создан")
    return RedirectResponse("/users", status_code=302)


@router.post("/{user_id}/change-password")
def users_change_password(
    user_id: int,
    request: Request,
    new_password: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        flash(request, "Пользователь не найден", "danger")
        return RedirectResponse("/users", status_code=302)
    user.password_hash = hash_password(new_password)
    db.commit()
    flash(request, f"Пароль для «{user.username}» обновлён")
    return RedirectResponse("/users", status_code=302)


@router.post("/{user_id}/delete")
def users_delete(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if user_id == current_user.id:
        flash(request, "Нельзя удалить самого себя", "danger")
        return RedirectResponse("/users", status_code=302)
    user = db.query(User).filter(User.id == user_id).first()
    if user:
        db.delete(user)
        db.commit()
        flash(request, f"Пользователь «{user.username}» удалён")
    return RedirectResponse("/users", status_code=302)
