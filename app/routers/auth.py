import secrets
import requests as http_requests
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from app.database import get_db
from app.auth import authenticate_user
from app.deps import flash, pop_flash
from app.models import User
from app.config import BITRIX24_CLIENT_ID, BITRIX24_CLIENT_SECRET, BITRIX24_PORTAL

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/login")
def login_page(request: Request):
    if request.session.get("user_id"):
        return RedirectResponse("/evaluations", status_code=302)
    return templates.TemplateResponse("auth/login.html", {
        "request": request,
        "flash": pop_flash(request),
        "bitrix24_enabled": bool(BITRIX24_CLIENT_ID),
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


@router.get("/auth/bitrix24")
def bitrix24_oauth_start(request: Request):
    if not BITRIX24_CLIENT_ID:
        flash(request, "Битрикс24 OAuth не настроен", "danger")
        return RedirectResponse("/login", status_code=302)
    state = secrets.token_urlsafe(16)
    request.session["oauth_state"] = state
    redirect_uri = _get_redirect_uri(request)
    auth_url = (
        f"https://{BITRIX24_PORTAL}/oauth/authorize/"
        f"?client_id={BITRIX24_CLIENT_ID}"
        f"&response_type=code"
        f"&redirect_uri={redirect_uri}"
        f"&state={state}"
    )
    return RedirectResponse(auth_url, status_code=302)


@router.get("/auth/bitrix24/callback")
def bitrix24_oauth_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db: Session = Depends(get_db),
):
    if error:
        flash(request, f"Ошибка авторизации Битрикс24: {error}", "danger")
        return RedirectResponse("/login", status_code=302)

    expected_state = request.session.pop("oauth_state", None)
    if not state or state != expected_state:
        flash(request, "Ошибка безопасности OAuth (state mismatch)", "danger")
        return RedirectResponse("/login", status_code=302)

    if not code:
        flash(request, "Код авторизации не получен", "danger")
        return RedirectResponse("/login", status_code=302)

    redirect_uri = _get_redirect_uri(request)
    token_resp = http_requests.post(
        "https://oauth.bitrix.info/oauth/token/",
        data={
            "grant_type": "authorization_code",
            "client_id": BITRIX24_CLIENT_ID,
            "client_secret": BITRIX24_CLIENT_SECRET,
            "code": code,
            "redirect_uri": redirect_uri,
        },
        timeout=10,
    )
    if token_resp.status_code != 200:
        flash(request, "Не удалось получить токен от Битрикс24", "danger")
        return RedirectResponse("/login", status_code=302)

    token_data = token_resp.json()
    access_token = token_data.get("access_token")
    if not access_token:
        flash(request, "Токен Битрикс24 пустой", "danger")
        return RedirectResponse("/login", status_code=302)

    user_resp = http_requests.get(
        f"https://{BITRIX24_PORTAL}/rest/user.current.json",
        params={"auth": access_token},
        timeout=10,
    )
    if user_resp.status_code != 200:
        flash(request, "Не удалось получить данные пользователя из Битрикс24", "danger")
        return RedirectResponse("/login", status_code=302)

    b24_user = user_resp.json().get("result", {})
    bitrix_id = str(b24_user.get("ID", ""))
    if not bitrix_id:
        flash(request, "ID пользователя Битрикс24 не получен", "danger")
        return RedirectResponse("/login", status_code=302)

    first = b24_user.get("NAME", "")
    last = b24_user.get("LAST_NAME", "")
    full_name = f"{first} {last}".strip() or b24_user.get("LOGIN", bitrix_id)
    email = b24_user.get("EMAIL", "")

    user = db.query(User).filter(User.bitrix_id == bitrix_id).first()
    if user is None:
        username_candidate = email or f"b24_{bitrix_id}"
        existing = db.query(User).filter(User.username == username_candidate).first()
        if existing:
            username_candidate = f"b24_{bitrix_id}"
        user = User(
            username=username_candidate,
            full_name=full_name,
            email=email or None,
            bitrix_id=bitrix_id,
            password_hash=None,
            is_active=True,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
    else:
        # обновляем имя и email при каждом входе
        user.full_name = full_name
        if email:
            user.email = email
        db.commit()

    if not user.is_active:
        flash(request, "Ваш аккаунт отключён. Обратитесь к администратору.", "danger")
        return RedirectResponse("/login", status_code=302)

    request.session["user_id"] = user.id
    return RedirectResponse("/evaluations", status_code=302)


def _get_redirect_uri(request: Request) -> str:
    base = str(request.base_url).rstrip("/")
    return f"{base}/auth/bitrix24/callback"
