import os
import asyncio
import logging
from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.database import engine, Base, SessionLocal
from app.routers import dashboard, devices, incidents
from app.routers.devices import audit_router

from app.services.checker import run_active_polling
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
app.include_router(audit_router)



async def background_polling_loop():
    """Background periodic loop running network active polling every 30 seconds."""
    logger.info("Starting background active polling loop (30s interval)...")
    while True:
        try:
            db = SessionLocal()
            await run_active_polling(db)
            db.close()
        except Exception as e:
            logger.error("Error encountered in background active polling: %s", e)
        await asyncio.sleep(30)


@app.on_event("startup")
async def startup_event():
    """Database initialization, seed check, and background task launch."""
    logger.info("Initializing Pertamina NetShield app...")
    Base.metadata.create_all(bind=engine)
    try:
        seed_data()
    except Exception as e:
        logger.info("Database seeding status: %s", e)

    # Start background active polling task
    asyncio.create_task(background_polling_loop())


@app.get("/")
def root_redirect():
    """Redirect root path to NOC dashboard."""
    return RedirectResponse(url="/noc")
