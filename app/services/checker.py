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

Two-tier inspection per perangkat:
  Tier 1 : Probe ketersediaan dasar (TCP socket) -> status UP / DOWN + RTT.
  Tier 2 : SNMP v2c deep metrics (CPU, memory, uptime, sysDescr) — HANYA bila
           host UP (ONLINE) dan device.snmp_enabled == True. Perangkat DOWN
           (OFFLINE) dilewati agar tidak membuang waktu menunggu timeout SNMP.
           SNMP dibatasi minimal SNMP_POLL_INTERVAL_SEC per perangkat (CPU Cisco
           adalah rata-rata 1 menit, polling tiap 5 detik tidak menambah informasi).

Keamanan SQLite (anti "database is locked"):
  - Tidak ada transaksi tulis yang dibiarkan terbuka selama menunggu I/O jaringan:
    evaluate_device_status_and_latency() sudah commit sebelum SNMP dimulai, hasil
    SNMP dikumpulkan dulu di memori, baru ditulis + commit dalam transaksi singkat.

Transisi status:
  UP  → DOWN  : set status DOWN, buat IncidentLog baru + kirim WA alert
  DOWN → UP   : set status UP, resolve insiden open, kirim WA recovery
"""

import asyncio
import logging
import os
import time
from datetime import datetime, timezone, timedelta
from typing import Dict

from sqlalchemy.exc import SQLAlchemyError

from app.database import SessionLocal
from app.models.monitoring import MonitoredService, IncidentLog, AreaZona  # noqa: F401
from app.services.ping import run_natural_probe
from app.services.notifier import send_incident_alert, send_recovery_alert  # noqa: F401
from app.utils.snmp import fetch_snmp_metrics

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Konstanta
# ---------------------------------------------------------------------------
POLL_INTERVAL_SEC = 5         # interval antar siklus polling (detik)
SNMP_TIMEOUT_SEC  = 2         # timeout per request SNMP (retries=1 di app/utils/snmp.py)
SNMP_POLL_INTERVAL_SEC = max(int(os.getenv("SNMP_POLL_INTERVAL_SEC", "30")), POLL_INTERVAL_SEC)
_WIB              = timezone(timedelta(hours=7))

# Waktu (monotonic) percobaan SNMP terakhir per device_id. Disimpan di memori — bukan di DB —
# agar kolom last_snmp_check tetap bermakna "poll SNMP terakhir yang BERHASIL", sementara
# perangkat yang gagal SNMP (community salah, agent mati) tidak dicoba ulang tiap 5 detik.
_snmp_last_attempt: Dict[int, float] = {}


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
      2. Natural socket probe tiap perangkat satu per satu (Tier 1)
      3. Evaluasi transisi status & update DB
      4. Jika device UP dan snmp_enabled=True → SNMP deep metrics fetch (Tier 2)
      5. db.commit()  →  db.close()
      6. Tidur POLL_INTERVAL_SEC detik
    """
    logger.info(
        "[NATURAL-PROBE] Background polling engine dimulai (interval=%ds, snmp_interval=%ds).",
        POLL_INTERVAL_SEC, SNMP_POLL_INTERVAL_SEC,
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

        except asyncio.CancelledError:
            db.rollback()
            raise
        except Exception as exc:
            db.rollback()
            logger.error("[NATURAL-PROBE ERROR] %s", exc, exc_info=True)
            print(f"[NATURAL-PROBE ERROR] {exc}")

        finally:
            db.close()

        await asyncio.sleep(POLL_INTERVAL_SEC)


from app.services.prom_poller import evaluate_device_status_and_latency  # noqa: E402


# ---------------------------------------------------------------------------
# _check_device  — evaluasi satu perangkat dengan Natural Socket Probing
# ---------------------------------------------------------------------------
async def _check_device(dev: MonitoredService, db) -> None:
    """
    Two-tier inspection untuk satu perangkat.

    Tier 1: TCP socket probing → evaluasi status & RTT threshold → update DB (commit
            dilakukan di dalam evaluate_device_status_and_latency).
    Tier 2: Hanya jika perangkat UP (ONLINE) dan snmp_enabled=True → SNMP deep metrics
            untuk memperbarui cpu_usage, memory_usage, uptime_str, sys_descr, last_snmp_check.
            Perangkat DOWN (OFFLINE) dilewati sepenuhnya.
    """
    # ── Tier 1: Availability probe ─────────────────────────────────────────
    is_up, latency, status_lbl, target_str = await run_natural_probe(dev.ip_address)
    status_val = 1 if is_up else 0
    rtt_ms = latency if is_up else None

    print(f"[NATURAL-PROBE] {target_str} -> {status_lbl} ({latency:.1f} ms) | State: {'UP' if is_up else 'DOWN'}")
    await evaluate_device_status_and_latency(db, dev, status_val, rtt_ms)

    # ── Tier 2: SNMP Deep Metrics (hanya jika device ONLINE dan SNMP dikonfigurasi) ──
    if not is_up:
        return  # OFFLINE → lewati SNMP (hemat resource, cegah blocking menunggu timeout)
    if not getattr(dev, "snmp_enabled", False):
        return
    if not _snmp_due(dev.id):
        return
    await _run_snmp_probe(dev, db)


def _snmp_due(device_id: int) -> bool:
    """True bila percobaan SNMP terakhir untuk perangkat ini sudah lewat SNMP_POLL_INTERVAL_SEC."""
    last = _snmp_last_attempt.get(device_id)
    return last is None or (time.monotonic() - last) >= SNMP_POLL_INTERVAL_SEC


async def _run_snmp_probe(dev: MonitoredService, db) -> None:
    """
    Jalankan SNMP v2c query untuk satu perangkat.
    Hanya dipanggil jika dev UP dan dev.snmp_enabled == True.

    Urutan aman untuk SQLite:
      1. Baca konfigurasi SNMP dari objek (tanpa menulis apa pun).
      2. await fetch_snmp_metrics() — I/O jaringan, TIDAK ada transaksi tulis terbuka.
      3. Tulis hasil ke kolom metrik + commit segera (transaksi tulis singkat).
    Error apa pun di sini di-rollback dan tidak menghentikan loop polling.
    """
    device_id = dev.id
    device_name = dev.name
    raw_ip = dev.ip_address or ""
    clean_ip = raw_ip.split(":")[0] if ":" in raw_ip else raw_ip
    community = getattr(dev, "snmp_community", None) or "public"
    port = getattr(dev, "snmp_port", None) or 161

    _snmp_last_attempt[device_id] = time.monotonic()

    try:
        result = await fetch_snmp_metrics(
            ip=clean_ip,
            community=community,
            port=port,
            timeout=SNMP_TIMEOUT_SEC,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # fetch_snmp_metrics tidak seharusnya raise; jaga-jaga
        logger.warning("[SNMP FAIL] %s (%s): %s", device_name, clean_ip, exc)
        return

    if not result.get("status"):
        logger.info(
            "[SNMP SKIP] %s (%s:%s): %s",
            device_name, clean_ip, port, result.get("error") or "no response",
        )
        return

    try:
        dev.last_snmp_check = _now_wib()
        if result.get("cpu_usage") is not None:
            dev.cpu_usage = result["cpu_usage"]
        if result.get("memory_usage") is not None:
            dev.memory_usage = result["memory_usage"]
        if result.get("uptime") is not None:
            dev.uptime_str = result["uptime"]
        if result.get("sys_descr") is not None:
            dev.sys_descr = result["sys_descr"]
        db.commit()

        logger.info(
            "[SNMP] %s (%s) | CPU=%s MEM=%s UP=%s",
            device_name, clean_ip,
            f"{dev.cpu_usage:.1f}%" if dev.cpu_usage is not None else "N/A",
            f"{dev.memory_usage:.1f}%" if dev.memory_usage is not None else "N/A",
            dev.uptime_str or "N/A",
        )
    except SQLAlchemyError as exc:
        db.rollback()
        logger.error("[SNMP DB ERROR] Gagal menyimpan metrik %s (%s): %s", device_name, clean_ip, exc)
