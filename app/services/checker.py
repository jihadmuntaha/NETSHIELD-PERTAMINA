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


from app.services.prom_poller import evaluate_device_status_and_latency


# ---------------------------------------------------------------------------
# _check_device  — evaluasi satu perangkat dengan Natural Socket Probing
# ---------------------------------------------------------------------------
async def _check_device(dev: MonitoredService, db) -> None:
    """
    Probe satu perangkat dengan TCP socket probing, evaluasi status & RTT threshold,
    dan update database secara presisi.
    """
    is_up, latency, status_lbl, target_str = await run_natural_probe(dev.ip_address)
    status_val = 1 if is_up else 0
    rtt_ms = latency if is_up else None

    print(f"[NATURAL-PROBE] {target_str} -> {status_lbl} ({latency:.1f} ms) | State: {'UP' if is_up else 'DOWN'}")
    await evaluate_device_status_and_latency(db, dev, status_val, rtt_ms)

