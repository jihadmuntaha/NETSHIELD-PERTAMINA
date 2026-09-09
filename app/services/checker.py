import asyncio
import re
import sys
import time
from datetime import datetime
import logging
from sqlalchemy.orm import Session

from app.models.monitoring import MonitoredService, IncidentLog, AreaZona
from app.services.notifier import send_wa_alert

logger = logging.getLogger(__name__)


async def ping_ip(ip_address: str) -> tuple[bool, float]:
    """
    Ping an IP address asynchronously using OS ping executable.
    Returns tuple of (is_up: bool, response_time_ms: float).
    """
    if sys.platform == "win32":
        cmd = ["ping", "-n", "1", "-w", "1000", ip_address]
    else:
        cmd = ["ping", "-c", "1", "-W", "1", ip_address]

    start_time = time.perf_counter()
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        elapsed_ms = (time.perf_counter() - start_time) * 1000.0

        if proc.returncode == 0:
            # Parse stdout to extract exact ping time if available
            output = stdout.decode("latin-1", errors="ignore")
            match = re.search(r"time[=<]\s*([\d.]+)\s*ms", output, re.IGNORECASE)
            if match:
                response_time = float(match.group(1))
            else:
                response_time = round(elapsed_ms, 2)
            return True, round(response_time, 2)
        else:
            return False, 0.0

    except Exception as e:
        logger.debug("Exception while pinging IP %s: %r", ip_address, e)
        return False, 0.0


async def check_single_service(service: MonitoredService, db: Session) -> None:
    """Helper function to ping and process a single monitored service."""
    if service.is_maintenance:
        logger.info("Skipping service %s (%s) due to active maintenance mode.", service.name, service.ip_address)
        return

    previous_status = service.status
    is_up, response_time_ms = await ping_ip(service.ip_address)
    new_status = "UP" if is_up else "DOWN"

    # Anti-Flapping / Incident Trigger Logic: Transition UP -> DOWN
    if previous_status == "UP" and new_status == "DOWN":
        area_str = service.area.value if hasattr(service.area, "value") else str(service.area)
        device_type_str = service.device_type.value if hasattr(service.device_type, "value") else str(service.device_type)

        # Determine severity
        if area_str == AreaZona.ZONA_4.value or "Zona 4" in area_str:
            severity = "DISASTER"
        else:
            severity = "HIGH"

        # Create IncidentLog entry
        incident = IncidentLog(
            service_id=service.id,
            severity=severity,
            status="NEW",
            created_at=datetime.utcnow()
        )
        db.add(incident)

        # Send async WA alert
        await send_wa_alert(
            area=area_str,
            device_type=device_type_str,
            name=service.name,
            ip_address=service.ip_address,
            severity=severity
        )

    # Update service attributes
    service.status = new_status
    service.response_time_ms = response_time_ms
    service.last_check = datetime.utcnow()


async def run_active_polling(db: Session) -> None:
    """
    Perform active network polling concurrently on all non-maintenance services.
    Triggers incident log creation and WA alerts on UP -> DOWN transition.
    """
    services = db.query(MonitoredService).all()
    if not services:
        return

    # Run pings concurrently for high performance
    tasks = [check_single_service(service, db) for service in services]
    await asyncio.gather(*tasks)

    db.commit()
