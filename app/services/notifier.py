import os
import sys
import logging
from datetime import datetime, timezone, timedelta

import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

FONNTE_API_URL = "https://api.fonnte.com/send"


def _resolve_target_for_device(device) -> str:
    """
    Pemetaan target WA dinamis berdasarkan Zona:
      - Zona 1 (Gate & Office)    -> WA_TARGET_ZONA_1 (fallback: WA_GROUP_OFFICE_SAFETY)
      - Zona 2 (Tank Farm)        -> WA_TARGET_ZONA_2 (fallback: WA_GROUP_TANK_MAINTENANCE)
      - Zona 3 (Filling Shed)     -> WA_TARGET_ZONA_3 (fallback: WA_GROUP_FILLING_SHED)
      - Zona 4 (Core IT)          -> WA_TARGET_ZONA_4 (fallback: WA_GROUP_IT_SUPPORT)
    """
    area_val = device.area.value if hasattr(device.area, "value") else str(device.area)
    target = ""

    if "Zona 1" in area_val or "Gate" in area_val:
        target = os.getenv("WA_TARGET_ZONA_1") or os.getenv("WA_GROUP_OFFICE_SAFETY") or ""
    elif "Zona 2" in area_val or "Tank" in area_val:
        target = os.getenv("WA_TARGET_ZONA_2") or os.getenv("WA_GROUP_TANK_MAINTENANCE") or ""
    elif "Zona 3" in area_val or "Filling" in area_val:
        target = os.getenv("WA_TARGET_ZONA_3") or os.getenv("WA_GROUP_FILLING_SHED") or ""
    elif "Zona 4" in area_val or "Server" in area_val or "IT" in area_val:
        target = os.getenv("WA_TARGET_ZONA_4") or os.getenv("WA_GROUP_IT_SUPPORT") or ""

    if not target:
        target = (
            os.getenv("WA_TARGET_DEFAULT")
            or os.getenv("WA_GROUP_MANAGER_DEPOT")
            or os.getenv("FONNTE_TARGET_PHONE")
            or ""
        )

    return target.strip()


def _get_wib_timestamp(dt: datetime | None = None) -> str:
    """Format timestamp dalam zona waktu WIB (UTC+7)."""
    wib_tz = timezone(timedelta(hours=7))
    if dt is None:
        dt = datetime.now(wib_tz)
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=wib_tz)
    else:
        dt = dt.astimezone(wib_tz)
    return dt.strftime("%Y-%m-%d %H:%M:%S WIB")


def _format_incident_message(device, incident) -> str:
    zona_name = device.area.value if hasattr(device.area, "value") else str(device.area)
    device_name = device.name
    ip_address = device.ip_address
    severity = getattr(incident, "severity", "HIGH") or "HIGH"
    incident_id = getattr(incident, "id", None) or "N/A"
    created_at = getattr(incident, "created_at", None)
    timestamp_wib = _get_wib_timestamp(created_at)

    return (
        "🚨 *PERTAMINA NETSHIELD - NETWORK ALERT* 🚨\n"
        f"*ID Insiden:* #{incident_id}\n"
        "*Lokasi:* Fuel Terminal Pengapon\n"
        f"*Zona:* {zona_name}\n"
        f"*Perangkat:* {device_name} ({ip_address})\n"
        "*Status:* DOWN (Unreachable)\n"
        f"*Severity:* {severity}\n"
        f"*Waktu Kejadian:* {timestamp_wib}\n\n"
        f"📱 *KLAIM CEPAT VIA WA:* Balas pesan ini dengan format:\n"
        f"`ACK {incident_id} <catatan>` (Contoh: `ACK {incident_id} Sedang penanganan di lokasi`)\n\n"
        "_Mohon tim PIC terkait segera melakukan pengecekan fisik atau klaim ACK pada dashboard NOC._\n"
        "Link Dashboard: http://localhost:5000/incidents"
    )


def _format_recovery_message(device, incident=None, latency: float = 0.0) -> str:
    zona_name = device.area.value if hasattr(device.area, "value") else str(device.area)
    device_name = device.name
    ip_address = device.ip_address

    lat_val = latency if latency and latency > 0 else getattr(device, "response_time_ms", 0.0)
    resolved_at = getattr(incident, "resolved_at", None) if incident else None
    timestamp_wib = _get_wib_timestamp(resolved_at)

    downtime_str = ""
    if incident and getattr(incident, "created_at", None) and getattr(incident, "resolved_at", None):
        delta = incident.resolved_at - incident.created_at
        total_seconds = max(0, int(delta.total_seconds()))
        minutes, seconds = divmod(total_seconds, 60)
        hours, minutes = divmod(minutes, 60)
        if hours > 0:
            downtime_str = f"{hours}j {minutes}m {seconds}d"
        elif minutes > 0:
            downtime_str = f"{minutes}m {seconds}d"
        else:
            downtime_str = f"{seconds}d"

    pic_name = getattr(incident, "ack_by", None) or getattr(incident, "acknowledged_by", None)

    msg_lines = [
        "✅ *PERTAMINA NETSHIELD - RESOLVED ALERT* ✅",
        "*Lokasi:* Fuel Terminal Pengapon",
        f"*Zona:* {zona_name}",
        f"*Perangkat:* {device_name} ({ip_address})",
        "*Status:* UP (Recovered)",
    ]
    if downtime_str:
        msg_lines.append(f"*Downtime:* {downtime_str}")
    if pic_name:
        msg_lines.append(f"*PIC Penangan:* {pic_name}")

    msg_lines.extend([
        f"*Latensi:* {lat_val:.1f} ms",
        f"*Waktu Pulih:* {timestamp_wib}\n",
        "_Insiden telah ditandai RESOLVED secara otomatis oleh sistem._"
    ])

    return "\n".join(msg_lines)


async def _send_fonnte_message(target: str, message: str, alert_type: str = "NOTIFICATION") -> bool:
    fonnte_token = os.getenv("FONNTE_TOKEN", "").strip()

    log_banner = (
        f"\n" + "=" * 50 + "\n"
        f"[FONNTE WA {alert_type}]\n"
        f"Target : {target or 'NO_TARGET_CONFIGURED'}\n"
        f"Message:\n{message}\n"
        + "=" * 50 + "\n"
    )
    try:
        print(log_banner)
    except UnicodeEncodeError:
        if hasattr(sys.stdout, "buffer"):
            sys.stdout.buffer.write(log_banner.encode("utf-8"))
            sys.stdout.buffer.flush()

    if not fonnte_token:
        logger.warning("[FONNTE] FONNTE_TOKEN tidak terkonfigurasi di environment.")
        return False

    if not target:
        logger.warning("[FONNTE] Target WA tidak ditemukan untuk zona ini.")
        return False

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                FONNTE_API_URL,
                headers={"Authorization": fonnte_token},
                data={
                    "target": target,
                    "message": message
                }
            )
            if response.status_code == 200:
                logger.info(
                    "[FONNTE SUCCESS] Notifikasi %s terkirim ke %s. (Response: %s)",
                    alert_type,
                    target,
                    response.text[:200]
                )
                return True
            else:
                logger.error(
                    "[FONNTE HTTP ERROR] %s ke %s (HTTP %d: %s)",
                    alert_type,
                    target,
                    response.status_code,
                    response.text[:200]
                )
                return False
    except Exception as exc:
        logger.error("[FONNTE EXCEPTION] Error saat mengirimi WA via Fonnte ke %s: %s", target, exc)
        print(f"[FONNTE ERROR] {exc}")
        return False


async def send_wa_reply(target: str, message: str) -> bool:
    """Kirim balasan konfirmasi pesan WhatsApp balik ke pengirim/teknisi via Fonnte."""
    return await _send_fonnte_message(target, message, alert_type="ACK REPLY CONFIRMATION")


def format_ack_confirmation(
    incident_id: int,
    device_name: str,
    ip_address: str,
    zone_name: str,
    acknowledged_by: str,
    acknowledged_at_formatted: str,
    ack_message: str,
) -> str:
    """Format pesan konfirmasi ACK insiden sesuai template Fonnte."""
    return (
        "✅ *PERTAMINA NETSHIELD - ACK CONFIRMED* ✅\n"
        f"*ID Insiden:* #{incident_id}\n"
        f"*Perangkat:* {device_name} ({ip_address})\n"
        f"*Zona:* {zone_name}\n"
        f"*PIC:* {acknowledged_by}\n"
        f"*Waktu ACK:* {acknowledged_at_formatted} WIB\n"
        f"*Catatan:* {ack_message}\n\n"
        "_Status di Dashboard NOC telah diperbarui._"
    )



_last_alert_sent = {}


def _is_alert_cooldown_active(dev_identifier: str, alert_type: str, cooldown_seconds: int = 60) -> bool:
    now = datetime.now()
    key = (dev_identifier, alert_type)
    last_sent = _last_alert_sent.get(key)
    if last_sent and (now - last_sent).total_seconds() < cooldown_seconds:
        return True
    _last_alert_sent[key] = now
    return False


async def send_incident_alert(device, incident) -> bool:
    """
    Kirim notifikasi WhatsApp alert (DOWN) via Fonnte API.
    Format pesan merah untuk kondisi DOWN.
    """
    dev_key = str(getattr(device, "id", None) or getattr(device, "ip_address", "unknown"))
    if _is_alert_cooldown_active(dev_key, "DOWN", cooldown_seconds=60):
        logger.warning("[FONNTE COOLDOWN] Notifikasi DOWN untuk perangkat %s diabaikan (cooldown 60s).", dev_key)
        return False

    target = _resolve_target_for_device(device)
    message = _format_incident_message(device, incident)
    return await _send_fonnte_message(target, message, alert_type="INCIDENT ALERT (DOWN)")


async def send_recovery_alert(device, incident=None, latency: float = 0.0) -> bool:
    """
    Kirim notifikasi WhatsApp recovery alert (UP) via Fonnte API.
    Format pesan hijau untuk kondisi RECOVERY (UP).
    """
    dev_key = str(getattr(device, "id", None) or getattr(device, "ip_address", "unknown"))
    if _is_alert_cooldown_active(dev_key, "UP", cooldown_seconds=60):
        logger.warning("[FONNTE COOLDOWN] Notifikasi UP untuk perangkat %s diabaikan (cooldown 60s).", dev_key)
        return False

    target = _resolve_target_for_device(device)
    message = _format_recovery_message(device, incident=incident, latency=latency)
    return await _send_fonnte_message(target, message, alert_type="RECOVERY ALERT (UP)")


# Legacy wrappers for backward compatibility
async def send_wa_alert(area: str, device_type: str, name: str, ip_address: str, severity: str) -> None:
    class DummyDevice:
        def __init__(self, name, ip_address, area, device_type):
            self.name = name
            self.ip_address = ip_address
            self.area = area
            self.device_type = device_type

    class DummyIncident:
        def __init__(self, severity):
            self.severity = severity
            self.created_at = datetime.now()

    await send_incident_alert(DummyDevice(name, ip_address, area, device_type), DummyIncident(severity))


async def send_wa_recovery(area: str, device_type: str, name: str, ip_address: str, latency_ms: float) -> None:
    class DummyDevice:
        def __init__(self, name, ip_address, area, device_type, response_time_ms):
            self.name = name
            self.ip_address = ip_address
            self.area = area
            self.device_type = device_type
            self.response_time_ms = response_time_ms

    await send_recovery_alert(DummyDevice(name, ip_address, area, device_type, latency_ms), latency=latency_ms)

