"""
app/services/checker.py
========================
Background polling engine untuk Pertamina NetShield.
Menggunakan Asynchronous Socket TCP Probing (Prometheus Blackbox Exporter style).

Arsitektur:
  - start_polling()  : loop utama, dipanggil dari main.py via asyncio.create_task()
  - Setiap iterasi membuka sesi DB baru + db.expire_all() agar perubahan
    IP/config yang dibuat via web UI selalu terbaca (tidak ada stale cache).
  - Loop sequential per device dengan natural socket probe.
  - Interval polling : 5 detik.

Transisi status:
  UP  → DOWN  : set status DOWN, buat IncidentLog baru + kirim WA alert
  DOWN → UP   : set status UP, resolve insiden open, kirim WA recovery
"""

import asyncio
import logging
from datetime import datetime, timezone, timedelta

from app.database import SessionLocal
from app.models.monitoring import MonitoredService, IncidentLog, AreaZona
from app.services.ping import run_natural_probe
from app.services.notifier import send_incident_alert, send_recovery_alert

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Konstanta
# ---------------------------------------------------------------------------
POLL_INTERVAL_SEC = 5         # interval antar siklus polling (detik)
_WIB              = timezone(timedelta(hours=7))


def _now_wib() -> datetime:
    """Kembalikan datetime naive WIB (UTC+7) untuk disimpan ke database."""
    return datetime.now(_WIB).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# start_polling  — entry point yang dipanggil main.py
# ---------------------------------------------------------------------------
async def start_polling() -> None:
    """
    Loop polling utama. Dipanggil sekali saat startup via asyncio.create_task().

    Setiap iterasi:
      1. Buka sesi DB baru  →  db.expire_all()  →  query perangkat aktif
      2. Natural socket probe tiap perangkat satu per satu
      3. Evaluasi transisi status & update DB
      4. db.commit()  →  db.close()
      5. Tidur POLL_INTERVAL_SEC detik
    """
    logger.info(
        "[NATURAL-PROBE] Background polling engine dimulai (interval=%ds).",
        POLL_INTERVAL_SEC,
    )

    while True:
        db = SessionLocal()
        try:
            # Paksa SQLAlchemy membaca ulang semua objek dari database
            db.expire_all()

            devices = (
                db.query(MonitoredService)
                .filter(MonitoredService.is_maintenance == False)  # noqa: E712
                .all()
            )

            if not devices:
                logger.info("[NATURAL-PROBE] Tidak ada perangkat aktif untuk dipoll.")
            else:
                for dev in devices:
                    if "9090" in str(dev.ip_address) or "prometheus" in str(dev.name).lower():
                        # Dipantau khusus oleh prom_poller.py
                        continue
                    await _check_device(dev, db)

            db.commit()

        except Exception as exc:
            db.rollback()
            logger.error("[NATURAL-PROBE ERROR] %s", exc, exc_info=True)
            print(f"[NATURAL-PROBE ERROR] {exc}")

        finally:
            db.close()

        await asyncio.sleep(POLL_INTERVAL_SEC)


# ---------------------------------------------------------------------------
# _check_device  — evaluasi satu perangkat dengan Natural Socket Probing
# ---------------------------------------------------------------------------
async def _check_device(dev: MonitoredService, db) -> None:
    """
    Probe satu perangkat dengan TCP socket probing, evaluasi transisi status,
    dan update database secara presisi.

    Console Logging Format:
      [NATURAL-PROBE] 127.0.0.1:8000 -> CONNECTED (1.4 ms) | State: UP
      [NATURAL-PROBE] 10.4.12.1:80 -> TIMEOUT | State: DOWN
    """
    is_up, latency, status_lbl, target_str = await run_natural_probe(dev.ip_address)

    area_str = dev.area.value if hasattr(dev.area, "value") else str(dev.area)
    dtype_str = (
        dev.device_type.value if hasattr(dev.device_type, "value") else str(dev.device_type)
    )
    prev_status = dev.status   # "UP" atau "DOWN"

    # ===================================================================== UP
    if is_up:
        dev.response_time_ms = latency
        dev.last_check = _now_wib()

        print(f"[NATURAL-PROBE] {target_str} -> {status_lbl} ({latency:.1f} ms) | State: UP")

        if prev_status == "DOWN":
            # ------------------------------------------------ DOWN → UP (RECOVERY)
            dev.status = "UP"

            open_incidents = (
                db.query(IncidentLog)
                .filter(
                    IncidentLog.service_id == dev.id,
                    IncidentLog.status.in_(["NEW", "ACKNOWLEDGED"]),
                )
                .all()
            )
            resolved_at = _now_wib()
            for inc in open_incidents:
                inc.status = "RESOLVED"
                inc.resolved_at = resolved_at

            print(
                f"[RECOVERY DETECTED] {dev.name} ({target_str}) is now UP! "
                f"({latency:.1f} ms, {len(open_incidents)} insiden di-resolve)"
            )

            last_inc = open_incidents[-1] if open_incidents else None
            await send_recovery_alert(
                device=dev,
                incident=last_inc,
                latency=latency,
            )

        else:
            # ------------------------------------------------ UP → UP (steady)
            dev.status = "UP"

    # =================================================================== DOWN
    else:
        dev.response_time_ms = 0.0
        dev.last_check       = _now_wib()

        print(f"[NATURAL-PROBE] {target_str} -> {status_lbl} | State: DOWN")

        if prev_status == "UP":
            # ----------------------------------------- UP → DOWN (INCIDENT)
            dev.status = "DOWN"

            severity = (
                "DISASTER"
                if area_str == AreaZona.ZONA_4.value or "Zona 4" in area_str
                else "HIGH"
            )

            new_incident = IncidentLog(
                service_id=dev.id,
                severity=severity,
                status="NEW",
                created_at=_now_wib(),
            )
            db.add(new_incident)
            db.flush()

            print(
                f"[INCIDENT CREATED] {dev.name} ({target_str}) is now DOWN! "
                f"severity={severity}"
            )

            await send_incident_alert(
                device=dev,
                incident=new_incident,
            )

        else:
            # Sudah DOWN dari sebelumnya — jaga status, jangan buat insiden duplikat
            dev.status = "DOWN"
