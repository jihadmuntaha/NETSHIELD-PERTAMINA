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


async def query_prometheus_latency(job_name: str = "pertamina_devices") -> dict:
    """
    Helper query metrik latensi Prometheus.
    Query PromQL: probe_duration_seconds{job="..."} atau fallback probe_duration_seconds.
    Konversi nilai detik ke milidetik: rtt_ms = round(float(val) * 1000, 2).
    """
    latency_map = {}
    async with httpx.AsyncClient(timeout=2.0) as client:
        try:
            res = await client.get(
                PROMETHEUS_QUERY_URL,
                params={"query": f'probe_duration_seconds{{job="{job_name}"}}'}
            )
            if res.status_code != 200 or not res.json().get("data", {}).get("result"):
                res = await client.get(
                    PROMETHEUS_QUERY_URL,
                    params={"query": "probe_duration_seconds"}
                )

            if res.status_code == 200:
                results = res.json().get("data", {}).get("result", [])
                for item in results:
                    metric = item.get("metric", {})
                    target_key = metric.get("device_name") or metric.get("name") or metric.get("instance") or metric.get("target")
                    raw_val = item.get("value", [None, None])
                    if target_key and raw_val[1] is not None:
                        try:
                            val_sec = float(raw_val[1])
                            rtt_ms = round(val_sec * 1000, 2)
                            latency_map[target_key] = rtt_ms
                            clean_ip = target_key.split(":")[0]
                            latency_map[clean_ip] = rtt_ms
                        except (ValueError, TypeError):
                            pass
        except Exception as exc:
            logger.debug("[PROM-LATENCY QUERY ERROR] %s", exc)
    return latency_map


async def evaluate_device_status_and_latency(db, service: MonitoredService, status_val: int, rtt_ms: float | None):
    """
    Evaluasi status perangkat (0=DOWN, 1=UP) dan latensi RTT (milidetik) sesuai aturan threshold:
    - Status == 0: Tiket CRITICAL ("Device DOWN", latency_ms=None) + Alert WA CRITICAL
    - Status == 1 & rtt_ms > 200.0: Tiket WARNING ("High Latency Detected ({rtt_ms} ms)", latency_ms=rtt_ms) + Alert WA WARNING
    - Status == 1 & rtt_ms <= 200.0: Auto-Recovery insiden WARNING/CRITICAL aktif -> RESOLVED + Alert WA Recovery
    """
    now = datetime.now()

    if status_val == 0:
        # =========================================================================
        # STATUS DOWN (0) -> Tiket CRITICAL
        # =========================================================================
        service.status = "DOWN"
        service.response_time_ms = 0.0
        service.last_check = now

        # Cek apakah sudah ada tiket aktif CRITICAL
        active_critical = (
            db.query(IncidentLog)
            .filter(
                IncidentLog.service_id == service.id,
                IncidentLog.status.in_(["NEW", "ACKNOWLEDGED"]),
                IncidentLog.severity == "CRITICAL"
            )
            .first()
        )

        if not active_critical:
            # Resolve tiket WARNING aktif sebelumnya jika ada (di-upgrade ke CRITICAL)
            active_warnings = (
                db.query(IncidentLog)
                .filter(
                    IncidentLog.service_id == service.id,
                    IncidentLog.status.in_(["NEW", "ACKNOWLEDGED"]),
                    IncidentLog.severity == "WARNING"
                )
                .all()
            )
            for w_inc in active_warnings:
                w_inc.status = "RESOLVED"
                w_inc.resolved_at = now

            new_inc = IncidentLog(
                service_id=service.id,
                severity="CRITICAL",
                title="Device DOWN",
                latency_ms=None,
                status="NEW",
                created_at=now
            )
            db.add(new_inc)
            db.commit()
            db.refresh(new_inc)

            logger.info(
                "[STATUS DOWN] Perangkat %s (%s) DOWN! Tiket CRITICAL #%d dibuat.",
                service.name, service.ip_address, new_inc.id
            )
            await send_incident_alert(device=service, incident=new_inc)
        else:
            db.commit()

    elif status_val == 1 and rtt_ms is not None and rtt_ms > 200.0:
        # =========================================================================
        # STATUS UP (1) & LATENCY > 200ms -> Tiket WARNING
        # =========================================================================
        service.status = "UP"
        service.response_time_ms = rtt_ms
        service.last_check = now

        # Cek apakah sudah ada tiket aktif berstatus WARNING
        active_warning = (
            db.query(IncidentLog)
            .filter(
                IncidentLog.service_id == service.id,
                IncidentLog.status.in_(["NEW", "ACKNOWLEDGED"]),
                IncidentLog.severity == "WARNING"
            )
            .first()
        )

        if not active_warning:
            new_inc = IncidentLog(
                service_id=service.id,
                severity="WARNING",
                title=f"High Latency Detected ({rtt_ms} ms)",
                latency_ms=rtt_ms,
                status="NEW",
                created_at=now
            )
            db.add(new_inc)
            db.commit()
            db.refresh(new_inc)

            logger.info(
                "[HIGH LATENCY WARNING] Perangkat %s (%s) merespons %s ms (>200ms). Tiket WARNING #%d dibuat.",
                service.name, service.ip_address, rtt_ms, new_inc.id
            )
            await send_incident_alert(device=service, incident=new_inc)
        else:
            active_warning.latency_ms = rtt_ms
            active_warning.title = f"High Latency Detected ({rtt_ms} ms)"
            db.commit()

    else:
        # =========================================================================
        # STATUS UP (1) & LATENCY <= 200ms -> NORMAL (AUTO-RECOVERY)
        # =========================================================================
        service.status = "UP"
        if rtt_ms is not None:
            service.response_time_ms = rtt_ms
        service.last_check = now

        active_incidents = (
            db.query(IncidentLog)
            .filter(
                IncidentLog.service_id == service.id,
                IncidentLog.status.in_(["NEW", "ACKNOWLEDGED"])
            )
            .all()
        )

        if active_incidents:
            resolved_dt = now
            for inc in active_incidents:
                inc.status = "RESOLVED"
                inc.resolved_at = resolved_dt

            db.commit()
            last_inc = active_incidents[-1]
            logger.info(
                "[AUTO-RECOVERY] Perangkat %s (%s) pulih normal (%s ms). Insiden di-resolve.",
                service.name, service.ip_address, rtt_ms if rtt_ms is not None else 0.0
            )
            await send_recovery_alert(device=service, incident=last_inc, latency=rtt_ms or service.response_time_ms or 0.0)
        else:
            db.commit()


async def _process_custom_target(db, device_name: str, zone: str, ip_address: str, status_val: int, rtt_ms: float | None = None):
    """Proses target metrik dari query Prometheus dengan evaluasi status & RTT threshold."""
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
            response_time_ms=rtt_ms or 0.0,
            is_maintenance=False
        )
        db.add(service)
        db.commit()

    await evaluate_device_status_and_latency(db, service, status_val, rtt_ms)


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

            # 4. Jika Server Prometheus hidup, proses target query tambahan beserta metrik latensi
            if is_prom_alive:
                latency_map = await query_prometheus_latency()
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

                                clean_ip = ip_address.split(":")[0] if ip_address else ""
                                rtt_ms = latency_map.get(ip_address) or latency_map.get(clean_ip) or latency_map.get(device_name)

                                if device_name and ip_address:
                                    await _process_custom_target(db, device_name, zone, ip_address, status_val, rtt_ms)
                    except Exception as err:
                        logger.debug("[PROM-TARGETS QUERY ERROR] %s", err)

        except Exception as exc:
            db.rollback()
            logger.error("[PROM-POLLER ERROR] %s", exc)
        finally:
            db.close()

        await asyncio.sleep(POLL_INTERVAL_SEC)
