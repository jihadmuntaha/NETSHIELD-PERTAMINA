import os
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.core.security import verify_credentials, get_current_user

router = APIRouter(tags=["Authentication"])

# Configure Templates Directory
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEMPLATES_DIR = os.path.join(BASE_DIR, "app", "templates")
if not os.path.exists(TEMPLATES_DIR):
    TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")

templates = Jinja2Templates(directory=TEMPLATES_DIR)


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    """
    GET /login: Render login.html page if unauthenticated.
    If already logged in, redirect to NOC dashboard.
    """
    if get_current_user(request):
        return RedirectResponse(url="/", status_code=303)

    error = request.query_params.get("error")
    error_msg = None
    if error:
        error_msg = "ID Operator atau Password salah."

    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            "request": request,
            "error": error_msg,
            "app_name": os.getenv("APP_NAME", "Pertamina NetShield")
        }
    )


@router.post("/login")
def login_action(
    request: Request,
    username: str = Form(...),
    password: str = Form(...)
):
    """
    POST /login: Validate Form credentials, store session, and redirect to dashboard.
    """
    if verify_credentials(username, password):
        request.session["user"] = username
        return RedirectResponse(url="/", status_code=303)

    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            "request": request,
            "error": "ID Operator atau Password tidak valid!",
            "username": username,
            "app_name": os.getenv("APP_NAME", "Pertamina NetShield")
        },
        status_code=400
    )


@router.get("/logout")
def logout_action(request: Request):
    """
    GET /logout: Clear session and redirect user to /login.
    """
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)
