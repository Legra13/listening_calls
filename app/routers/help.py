from fastapi import APIRouter, Depends, Request
from fastapi.templating import Jinja2Templates
from app.models import User
from app.deps import get_current_user, pop_flash

router = APIRouter(prefix="/help")
templates = Jinja2Templates(directory="app/templates")


@router.get("")
def help_index(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    return templates.TemplateResponse("help/index.html", {
        "request": request,
        "current_user": current_user,
        "flash": pop_flash(request),
    })
