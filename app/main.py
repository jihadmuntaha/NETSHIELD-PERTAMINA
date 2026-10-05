import os
import asyncio
import logging
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.database import engine, Base, run_migrations
from app.routers import dashboard, devices, incidents, webhook, auth
from app.routers.devices import audit_router

from app.services.checker import start_polling
from app.services.prom_poller import monitor_prometheus_targets
from seed import seed_data

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("netshield_main")

app = FastAPI(
    title="Pertamina NetShield",
    description="Pilot Project NMS khusus FT Pengapon Semarang",
    version="1.0.0"
)

# Global Authentication Protection Middleware
@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    # Whitelist endpoints: /login, /static, /webhook/whatsapp (and any /webhook)
    whitelist_prefixes = ["/login", "/static", "/webhook"]
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

# Include Routers
app.include_router(auth.router)
app.include_router(dashboard.router)
app.include_router(devices.router)
app.include_router(incidents.router)
app.include_router(incidents.v1_router)
app.include_router(webhook.router)
app.include_router(audit_router)



@app.on_event("startup")
async def on_startup():
    """Database initialization, seed check, and background polling task launch."""
    logger.info("Initializing Pertamina NetShield app...")
    Base.metadata.create_all(bind=engine)
    run_migrations()
    try:
        seed_data()
    except Exception as e:
        logger.info("Database seeding status: %s", e)

    # Jalankan background polling sebagai asyncio task non-blocking
    asyncio.create_task(start_polling())
    asyncio.create_task(monitor_prometheus_targets())


@app.get("/")
def root_redirect():
    """Redirect root path to NOC dashboard."""
    return RedirectResponse(url="/noc")
