from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import User
from app.auth import create_user, hash_password
from app.deps import get_admin_user, flash, pop_flash

router = APIRouter(prefix="/users")
templates = Jinja2Templates(directory="app/templates")

ROLE_LABELS = {"owner": "владелец", "admin": "админ", "user": "сотрудник"}


def _can_manage(current_user: User, target: User) -> bool:
    """Админ управляет только сотрудниками; владелец — всеми."""
    return current_user.is_owner or target.role == "user"


@router.get("")
def users_list(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    users = db.query(User).order_by(User.created_at).all()
    return templates.TemplateResponse("users/index.html", {
        "request": request,
        "current_user": current_user,
        "users": users,
        "role_labels": ROLE_LABELS,
        "flash": pop_flash(request),
    })


@router.post("/create")
def users_create(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    full_name: str = Form(default=""),
    role: str = Form(default="user"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    username = username.strip()
    if role not in ("user", "admin") or (role == "admin" and not current_user.is_owner):
        role = "user"
    existing = db.query(User).filter(User.username == username).first()
    if existing:
        flash(request, f"Пользователь «{username}» уже существует", "danger")
        return RedirectResponse("/users", status_code=302)
    user = create_user(db, username, password)
    user.role = role
    if "@" in username:
        user.email = username
    if full_name.strip():
        user.full_name = full_name.strip()
    db.commit()
    flash(request, f"Пользователь «{full_name or username}» создан")
    return RedirectResponse("/users", status_code=302)


@router.post("/{user_id}/change-password")
def users_change_password(
    user_id: int,
    request: Request,
    new_password: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        flash(request, "Пользователь не найден", "danger")
        return RedirectResponse("/users", status_code=302)
    if user.id != current_user.id and not _can_manage(current_user, user):
        flash(request, "Недостаточно прав", "danger")
        return RedirectResponse("/users", status_code=302)
    user.password_hash = hash_password(new_password)
    db.commit()
    flash(request, f"Пароль для «{user.username}» обновлён")
    return RedirectResponse("/users", status_code=302)


@router.post("/{user_id}/toggle-active")
def users_toggle_active(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    if user_id == current_user.id:
        flash(request, "Нельзя деактивировать себя", "danger")
        return RedirectResponse("/users", status_code=302)
    user = db.query(User).filter(User.id == user_id).first()
    if user and not _can_manage(current_user, user):
        flash(request, "Недостаточно прав", "danger")
        return RedirectResponse("/users", status_code=302)
    if user:
        user.is_active = not user.is_active
        db.commit()
        status = "активирован" if user.is_active else "деактивирован"
        flash(request, f"Пользователь «{user.full_name or user.username}» {status}")
    return RedirectResponse("/users", status_code=302)


@router.post("/{user_id}/delete")
def users_delete(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    if user_id == current_user.id:
        flash(request, "Нельзя удалить самого себя", "danger")
        return RedirectResponse("/users", status_code=302)
    user = db.query(User).filter(User.id == user_id).first()
    if user and not _can_manage(current_user, user):
        flash(request, "Недостаточно прав", "danger")
        return RedirectResponse("/users", status_code=302)
    if user:
        db.delete(user)
        db.commit()
        flash(request, f"Пользователь «{user.username}» удалён")
    return RedirectResponse("/users", status_code=302)


@router.post("/{user_id}/set-role")
def users_set_role(
    user_id: int,
    request: Request,
    role: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    if not current_user.is_owner:
        flash(request, "Выдавать права админа может только владелец", "danger")
        return RedirectResponse("/users", status_code=302)
    user = db.query(User).filter(User.id == user_id).first()
    if not user or user.is_owner or role not in ("user", "admin"):
        flash(request, "Нельзя изменить роль этого пользователя", "danger")
        return RedirectResponse("/users", status_code=302)
    user.role = role
    db.commit()
    flash(request, f"«{user.full_name or user.username}»: роль — {ROLE_LABELS[role]}")
    return RedirectResponse("/users", status_code=302)
