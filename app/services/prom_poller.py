import asyncio
import logging
from datetime import datetime
import httpx

from app.database import SessionLocal
from app.models.monitoring import MonitoredService, IncidentLog, AreaZona, DeviceType
from app.services.notifier import send_incident_alert, send_recovery_alert

logger = logging.getLogger("netshield_prom_poller")

PROMETHEUS_SERVER_URL = "http://127.0.0.1:9090"
PROMETHEUS_QUERY_URL = "http://127.0.0.1:9090/api/v1/query"
POLL_INTERVAL_SEC = 3


def _match_area_zone(zone_str: str) -> AreaZona:
    """Map string nama zona ke AreaZona Enum."""
    if not zone_str:
        return AreaZona.ZONA_1
    for a in AreaZona:
        if a.value == zone_str or a.name == zone_str or zone_str in a.value:
            return a
    if "1" in zone_str or "Gate" in zone_str:
        return AreaZona.ZONA_1
    if "2" in zone_str or "Tank" in zone_str:
        return AreaZona.ZONA_2
    if "3" in zone_str or "Filling" in zone_str:
        return AreaZona.ZONA_3
    if "4" in zone_str or "IT" in zone_str or "Server" in zone_str:
        return AreaZona.ZONA_4
    return AreaZona.ZONA_1


async def monitor_prometheus_targets():
    """
    Background worker Prometheus poller.
    Memantau server Prometheus di http://127.0.0.1:9090 secara real-time.
    Ketika server Prometheus dinyalakan/dimatikan di CMD, status perangkat terkait
    akan berubah (UP/DOWN) secara instan dan mengirim notifikasi WhatsApp TEPAT 1X.
    """
    logger.info("[PROM-POLLER] Background worker Prometheus poller aktif (interval=3s).")

    while True:
        db = SessionLocal()
        try:
            db.expire_all()

            # 1. Cari perangkat yang menggunakan endpoint Prometheus (IP 127.0.0.1:9090 atau nama Prometheus Node)
            prom_service = (
                db.query(MonitoredService)
                .filter(
                    (MonitoredService.ip_address == "127.0.0.1:9090")
                    | (MonitoredService.ip_address == "http://127.0.0.1:9090")
                    | (MonitoredService.name.like("%Prometheus%"))
                )
                .first()
            )

            # 2. Cek apakah Server Prometheus di CMD sedang hidup (UP) atau mati (DOWN)
            is_prom_alive = False
            async with httpx.AsyncClient(timeout=2.0) as client:
                try:
                    res = await client.get(f"{PROMETHEUS_SERVER_URL}/api/v1/query", params={"query": "up"})
                    if res.status_code == 200:
                        is_prom_alive = True
                except Exception:
                    is_prom_alive = False

            # 3. Evaluasi status perangkat simulasi Prometheus (Stateful Transition)
            if prom_service:
                prev_status = prom_service.status

                if is_prom_alive:
                    # =========================================================
                    # SERVER PROMETHEUS NYALA (UP)
                    # =========================================================
                    prom_service.status = "UP"
                    prom_service.last_check = datetime.now()
                    prom_service.response_time_ms = 1.8

                    if prev_status == "DOWN":
                        # Transisi DOWN -> UP (Hanya kirim Notifikasi RECOVERY 1X)
                        open_incidents = (
                            db.query(IncidentLog)
                            .filter(
                                IncidentLog.service_id == prom_service.id,
                                IncidentLog.status.in_(["NEW", "ACKNOWLEDGED"])
                            )
                            .all()
                        )
                        resolved_dt = datetime.now()
                        for inc in open_incidents:
                            inc.status = "RESOLVED"
                            inc.resolved_at = resolved_dt

                        db.commit()
                        logger.info(
                            "[PROM-POLLER] Server Prometheus NYALA! Status %s (%s) menjadi UP.",
                            prom_service.name,
                            prom_service.ip_address
                        )

                        last_inc = open_incidents[-1] if open_incidents else None
                        await send_recovery_alert(device=prom_service, incident=last_inc)
                    else:
                        db.commit()

                else:
                    # =========================================================
                    # SERVER PROMETHEUS MATI / KILLED IN CMD (DOWN)
                    # =========================================================
                    prom_service.status = "DOWN"
                    prom_service.last_check = datetime.now()
                    prom_service.response_time_ms = 0.0

                    if prev_status == "UP":
                        # Transisi UP -> DOWN (Hanya buat insiden & kirim WA Alert 1X)
                        area_str = prom_service.area.value if hasattr(prom_service.area, "value") else str(prom_service.area)
                        severity = "DISASTER" if "Zona 4" in area_str else "HIGH"

                        new_incident = IncidentLog(
                            service_id=prom_service.id,
                            severity=severity,
                            status="NEW",
                            created_at=datetime.now()
                        )
                        new_incident.is_acknowledged = False

                        db.add(new_incident)
                        db.commit()
                        db.refresh(new_incident)

                        logger.info(
                            "[PROM-POLLER] Server Prometheus MATI! Status %s (%s) menjadi DOWN. Insiden #%d dibuat.",
                            prom_service.name,
                            prom_service.ip_address,
                            new_incident.id
                        )

                        await send_incident_alert(device=prom_service, incident=new_incident)
                    else:
                        # Tetap DOWN jika memang mati — jangan kirim notifikasi ulang/spam!
                        db.commit()

            # 4. Jika Server Prometheus hidup, proses target query tambahan jika ada
            if is_prom_alive:
                async with httpx.AsyncClient(timeout=2.0) as client:
                    try:
                        q_res = await client.get(PROMETHEUS_QUERY_URL, params={"query": 'up{job="pertamina_devices"}'})
                        if q_res.status_code == 200:
                            results = q_res.json().get("data", {}).get("result", [])
                            for item in results:
                                metric = item.get("metric", {})
                                device_name = metric.get("device_name") or metric.get("name") or metric.get("instance")
                                zone = metric.get("zone") or metric.get("area") or "Zona 1 - Gate & Main Office"
                                ip_address = metric.get("ip_address") or metric.get("instance")
                                raw_val = item.get("value", [None, 0])
                                try:
                                    status_val = int(raw_val[1])
                                except (TypeError, ValueError):
                                    status_val = 0

                                if device_name and ip_address:
                                    await _process_custom_target(db, device_name, zone, ip_address, status_val)
                    except Exception:
                        pass

        except Exception as exc:
            db.rollback()
            logger.error("[PROM-POLLER ERROR] %s", exc)
        finally:
            db.close()

        await asyncio.sleep(POLL_INTERVAL_SEC)


async def _process_custom_target(db, device_name: str, zone: str, ip_address: str, status_val: int):
    """Proses target metrik opsional dari query Prometheus."""
    clean_ip = ip_address.split(":")[0] if ":" in ip_address else ip_address
    service = (
        db.query(MonitoredService)
        .filter(
            (MonitoredService.ip_address == ip_address)
            | (MonitoredService.ip_address == clean_ip)
            | (MonitoredService.name == device_name)
        )
        .first()
    )
    if not service:
        area_enum = _match_area_zone(zone)
        service = MonitoredService(
            name=device_name,
            ip_address=ip_address,
            area=area_enum,
            device_type=DeviceType.ROUTER,
            status="UP" if status_val == 1 else "DOWN",
            response_time_ms=0.0,
            is_maintenance=False
        )
        db.add(service)
        db.commit()

    prev_status = service.status
    if status_val == 0 and prev_status == "UP":
        service.status = "DOWN"
        new_inc = IncidentLog(service_id=service.id, severity="HIGH", status="NEW", created_at=datetime.now())
        db.add(new_inc)
        db.commit()
        await send_incident_alert(device=service, incident=new_inc)
    elif status_val == 1 and prev_status == "DOWN":
        service.status = "UP"
        active_incidents = db.query(IncidentLog).filter(IncidentLog.service_id == service.id, IncidentLog.status.in_(["NEW", "ACKNOWLEDGED"])).all()
        for inc in active_incidents:
            inc.status = "RESOLVED"
            inc.resolved_at = datetime.now()
        db.commit()
        await send_recovery_alert(device=service, incident=active_incidents[-1] if active_incidents else None)
