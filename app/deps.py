from fastapi import Depends, Request
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import User


class NotAuthenticatedException(Exception):
    pass


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    user_id = request.session.get("user_id")
    if not user_id:
        raise NotAuthenticatedException()
    user = db.query(User).filter(User.id == user_id, User.is_active == True).first()
    if not user:
        request.session.clear()
        raise NotAuthenticatedException()
    return user


def flash(request: Request, message: str, category: str = "success"):
    request.session["flash"] = {"message": message, "category": category}


def pop_flash(request: Request) -> dict | None:
    return request.session.pop("flash", None)
