"""
app/routers/toolkit.py
======================
NOC Diagnostic Toolkit - Live ICMP Ping dari browser.

- GET  /toolkit           -> halaman terminal web (toolkit.html)
- POST /toolkit/api/ping  -> eksekusi ping, kembalikan JSON hasil + raw output
"""
import logging
import os
import platform
import threading
import time
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Request, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Device
from app.utils.ping import (
    MAX_COUNT,
    MIN_COUNT,
    InvalidPingTarget,
    build_ping_command,
    execute_icmp_ping,
    normalize_ping_target,
)

logger = logging.getLogger("netshield_toolkit")

router = APIRouter(prefix="/toolkit", tags=["NOC Toolkit"])

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEMPLATES_DIR = os.path.join(BASE_DIR, "app", "templates")
if not os.path.exists(TEMPLATES_DIR):
    TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
templates = Jinja2Templates(directory=TEMPLATES_DIR)

PACKET_OPTIONS = (1, 4, 8)
DEFAULT_TIMEOUT_MS = 1000
# Batasi jumlah proses ping simultan agar server tidak dibanjiri subprocess
MAX_CONCURRENT_PROBES = int(os.getenv("TOOLKIT_MAX_CONCURRENT_PINGS", "4"))
_probe_slots = threading.BoundedSemaphore(MAX_CONCURRENT_PROBES)


def _session_user(request: Request) -> Optional[str]:
    return request.session.get("user") if "session" in request.scope else None


def _error(status_code: int, target: str, message: str) -> JSONResponse:
    """Respons error dengan bentuk yang sama seperti respons sukses (mudah dirender UI)."""
    return JSONResponse(
        status_code=status_code,
        content={
            "target": target,
            "is_alive": False,
            "avg_latency_ms": None,
            "raw_output": f"[NETSHIELD] {message}",
            "error": message,
        },
    )


async def _read_ping_payload(request: Request) -> Dict[str, Any]:
    """Terima JSON maupun form-data / x-www-form-urlencoded."""
    content_type = request.headers.get("content-type", "")
    try:
        if "application/json" in content_type:
            data = await request.json()
            return data if isinstance(data, dict) else {}
        form = await request.form()
        return dict(form)
    except Exception:
        return {}


@router.get("", response_class=HTMLResponse)
def toolkit_page(request: Request, target: str = "", db: Session = Depends(get_db)):
    """Halaman NOC Diagnostic Toolkit (Live ICMP Ping)."""
    user = _session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    devices = (
        db.query(Device)
        .order_by(Device.location_name.asc(), Device.level.asc(), Device.name.asc())
        .all()
    )

    return templates.TemplateResponse(
        request=request,
        name="toolkit.html",
        context={
            "active_page": "toolkit",
            "user": user,
            "devices": devices,
            "packet_options": PACKET_OPTIONS,
            "prefill_target": target.strip()[:255],
            "server_os": f"{platform.system()} {platform.release()}".strip() or "Unknown",
            "app_name": os.getenv("APP_NAME", "Pertamina NetShield"),
        },
    )


@router.post("/api/ping")
async def api_ping(request: Request):
    """
    Jalankan ICMP ping ke ``target_ip`` sebanyak ``count`` paket.

    Body (JSON atau form): ``{"target_ip": "10.4.12.1", "count": 4}``

    Response: ``{"target", "is_alive", "avg_latency_ms", "raw_output", "command", "duration_ms"}``
    """
    user = _session_user(request)
    if not user:
        return _error(status.HTTP_401_UNAUTHORIZED, "", "Sesi login berakhir. Silakan login ulang.")

    payload = await _read_ping_payload(request)
    raw_target = str(payload.get("target_ip", "") or "").strip()

    try:
        count = int(payload.get("count", 4) or 4)
    except (TypeError, ValueError):
        return _error(status.HTTP_422_UNPROCESSABLE_ENTITY, raw_target, "Jumlah paket harus berupa angka.")
    if not MIN_COUNT <= count <= MAX_COUNT:
        return _error(status.HTTP_422_UNPROCESSABLE_ENTITY, raw_target, f"Jumlah paket harus {MIN_COUNT}-{MAX_COUNT}.")

    try:
        target = normalize_ping_target(raw_target)
    except InvalidPingTarget as exc:
        return _error(status.HTTP_400_BAD_REQUEST, raw_target, str(exc))

    if not _probe_slots.acquire(blocking=False):
        return _error(
            status.HTTP_429_TOO_MANY_REQUESTS,
            target,
            f"Terlalu banyak probe berjalan bersamaan (maks {MAX_CONCURRENT_PROBES}). Coba lagi sebentar.",
        )

    started = time.perf_counter()
    try:
        # subprocess bersifat blocking -> jalankan di threadpool agar event loop tetap responsif
        is_alive, avg_latency, raw_output = await run_in_threadpool(
            execute_icmp_ping, target, count, DEFAULT_TIMEOUT_MS
        )
    except InvalidPingTarget as exc:
        return _error(status.HTTP_400_BAD_REQUEST, raw_target, str(exc))
    except Exception:
        logger.exception("ICMP ping ke %s gagal", target)
        return _error(status.HTTP_500_INTERNAL_SERVER_ERROR, target, "Terjadi kesalahan internal saat menjalankan ping.")
    finally:
        _probe_slots.release()

    duration_ms = round((time.perf_counter() - started) * 1000)
    logger.info("[TOOLKIT] %s ping %s x%d -> %s (%s ms)", user, target, count, "UP" if is_alive else "DOWN", avg_latency)

    return {
        "target": target,
        "is_alive": is_alive,
        "avg_latency_ms": avg_latency,
        "raw_output": raw_output,
        "command": " ".join(build_ping_command(target, count, DEFAULT_TIMEOUT_MS)),
        "duration_ms": duration_ms,
    }
