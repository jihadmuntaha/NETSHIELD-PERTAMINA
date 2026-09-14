import os
import asyncio
import logging
from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.database import engine, Base
from app.routers import dashboard, devices, incidents, webhook
from app.routers.devices import audit_router

from app.services.checker import start_polling
from seed import seed_data

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("netshield_main")

app = FastAPI(
    title="Pertamina NetShield",
    description="Pilot Project NMS khusus FT Pengapon Semarang",
    version="1.0.0"
)

# Mount static directory if present
static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

# Include Routers
app.include_router(dashboard.router)
app.include_router(devices.router)
app.include_router(incidents.router)
app.include_router(webhook.router)
app.include_router(audit_router)


@app.on_event("startup")
async def on_startup():
    """Database initialization, seed check, and background polling task launch."""
    logger.info("Initializing Pertamina NetShield app...")
    Base.metadata.create_all(bind=engine)
    try:
        seed_data()
    except Exception as e:
        logger.info("Database seeding status: %s", e)

    # Jalankan background polling sebagai asyncio task non-blocking
    asyncio.create_task(start_polling())


@app.get("/")
def root_redirect():
    """Redirect root path to NOC dashboard."""
    return RedirectResponse(url="/noc")
