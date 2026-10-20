import os
import asyncio
import logging
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app.database import engine, Base, run_migrations
from app.routers import dashboard, devices, incidents, webhook, auth
from app.routers.devices import audit_router

from app.services.checker import start_polling
from app.services.prom_poller import monitor_prometheus_targets
from seed_pertamina_jbt import seed_database

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("netshield_main")

app = FastAPI(
    title="Pertamina NetShield NOC",
    description="Enterprise Multi-location Network Operations Center (Pertamina Patra Niaga Region Jawa Bagian Tengah)",
    version="2.0.0"
)

# Global Authentication Protection Middleware
@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    # Whitelist endpoints: /login, /static, webhook WhatsApp (Fonnte memanggil tanpa session).
    # Endpoint lain (termasuk /devices/upload-csv & /incidents/export/csv) wajib login.
    whitelist_prefixes = ["/login", "/static", "/webhook", "/api/v1/webhook"]
    is_whitelisted = any(path.startswith(prefix) for prefix in whitelist_prefixes) or path == "/favicon.ico"

    if is_whitelisted:
        return await call_next(request)

    # Check operator session
    user = request.session.get("user") if hasattr(request, "session") and request.session else None
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    return await call_next(request)

# Register SessionMiddleware (SECRET_KEY from env or default fallback)
SECRET_KEY = os.getenv("SECRET_KEY", "netshield-secret-key-pertamina-noc-2026")
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)

# Mount static directory if present
static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

# Templates directory (selaras dengan router lain: app/templates, fallback ke templates/)
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES_DIR = os.path.join(BASE_DIR, "app", "templates")
if not os.path.exists(TEMPLATES_DIR):
    TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
templates = Jinja2Templates(directory=TEMPLATES_DIR)

# Include Routers
app.include_router(auth.router)
app.include_router(dashboard.router)
app.include_router(devices.router)
app.include_router(devices.web_router)
app.include_router(incidents.router)
app.include_router(incidents.api_router)
app.include_router(incidents.v1_router)
app.include_router(webhook.router)
app.include_router(audit_router)


@app.on_event("startup")
async def on_startup():
    """Database initialization, seed check, and background polling task launch."""
    logger.info("Initializing Pertamina NetShield NOC enterprise application...")
    Base.metadata.create_all(bind=engine)
    run_migrations()
    try:
        from app.database import SessionLocal
        from app.models import Device
        db = SessionLocal()
        count = db.query(Device).count()
        db.close()
        if count == 0:
            logger.info("Seeding Pertamina JBT multi-location topology database...")
            seed_database()
        else:
            logger.info("Database already initialized with %d devices.", count)
    except Exception as e:
        logger.info("Database seeding status check: %s", e)

    # Launch non-blocking background polling tasks
    asyncio.create_task(start_polling())
    asyncio.create_task(monitor_prometheus_targets())


def _session_user(request: Request):
    """Ambil username operator dari session (None jika belum login)."""
    return request.session.get("user") if "session" in request.scope else None


def _route_exists(path: str) -> bool:
    """Cek apakah sebuah path sudah terdaftar di aplikasi (untuk modul opsional di portal)."""
    return any(getattr(route, "path", None) == path for route in app.routes)


@app.get("/")
def root_redirect(request: Request):
    """Root: operator yang sudah login diarahkan ke Portal Hub, selain itu ke halaman login."""
    if _session_user(request):
        return RedirectResponse(url="/portal", status_code=303)
    return RedirectResponse(url="/login", status_code=303)


@app.get("/portal", response_class=HTMLResponse)
def portal_hub(request: Request):
    """Portal Gateway / Command Center Hub - halaman pemilihan modul kerja setelah login."""
    user = _session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    return templates.TemplateResponse(
        request=request,
        name="portal.html",
        context={
            "active_page": "portal",
            "user": user,
            "toolkit_available": _route_exists("/toolkit"),
            "app_name": os.getenv("APP_NAME", "Pertamina NetShield"),
        },
    )
