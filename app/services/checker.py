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
  - Jika device UP dan snmp_enabled=True → jalankan SNMP fetch (non-blocking).
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
      4. Jika device UP dan snmp_enabled=True → SNMP deep metrics fetch
      5. db.commit()  →  db.close()
      6. Tidur POLL_INTERVAL_SEC detik
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
    update database, lalu — jika perangkat UP dan snmp_enabled=True — jalankan
    SNMP deep metrics fetch untuk memperbarui cpu_usage, memory_usage, uptime_str,
    sys_descr, dan last_snmp_check.
    """
    is_up, latency, status_lbl, target_str = await run_natural_probe(dev.ip_address)
    status_val = 1 if is_up else 0
    rtt_ms = latency if is_up else None

    print(f"[NATURAL-PROBE] {target_str} -> {status_lbl} ({latency:.1f} ms) | State: {'UP' if is_up else 'DOWN'}")
    await evaluate_device_status_and_latency(db, dev, status_val, rtt_ms)

    # ── SNMP Deep Metrics (hanya jika device UP dan SNMP dikonfigurasi) ────
    if is_up and getattr(dev, "snmp_enabled", False):
        await _run_snmp_probe(dev, db)


async def _run_snmp_probe(dev: MonitoredService, db) -> None:
    """
    Jalankan SNMP v2c query untuk satu perangkat.
    Hanya dipanggil jika dev.status UP dan dev.snmp_enabled == True.
    Hasil ditulis langsung ke kolom SNMP di baris device (tanpa commit — commit
    dilakukan oleh start_polling setelah seluruh device selesai di-probe).
    """
    try:
        from app.utils.snmp import fetch_snmp_metrics

        clean_ip = dev.ip_address.split(":")[0] if ":" in dev.ip_address else dev.ip_address
        community = getattr(dev, "snmp_community", "public") or "public"
        port      = getattr(dev, "snmp_port", 161) or 161

        result = await fetch_snmp_metrics(
            ip=clean_ip,
            community=community,
            port=port,
            timeout=2,
        )

        now = _now_wib()
        dev.last_snmp_check = now

        if result["status"]:
            if result["cpu_usage"] is not None:
                dev.cpu_usage = result["cpu_usage"]
            if result["memory_usage"] is not None:
                dev.memory_usage = result["memory_usage"]
            if result["uptime"] is not None:
                dev.uptime_str = result["uptime"]
            if result["sys_descr"] is not None:
                dev.sys_descr = result["sys_descr"]

            logger.info(
                "[SNMP] %s (%s) | CPU=%.1f%% MEM=%.1f%% UP=%s",
                dev.name, clean_ip,
                dev.cpu_usage if dev.cpu_usage is not None else -1,
                dev.memory_usage if dev.memory_usage is not None else -1,
                dev.uptime_str or "N/A",
            )
        else:
            logger.debug(
                "[SNMP SKIP] %s (%s): %s",
                dev.name, clean_ip, result.get("error", "no response")
            )


