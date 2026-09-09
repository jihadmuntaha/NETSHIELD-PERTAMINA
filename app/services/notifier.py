import os
import sys
import logging
from datetime import datetime, timezone, timedelta

import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

FONNTE_API_URL = "https://api.fonnte.com/send"

AREA_WA_GROUP_MAP = {
    "Zona 1 - Gate & Main Office": "WA_GROUP_OFFICE_SAFETY",
    "Zona 2 - Tank Farm (Tangki Timbun)": "WA_GROUP_TANK_MAINTENANCE",
    "Zona 3 - Filling Shed (Loading Bay)": "WA_GROUP_FILLING_SHED",
    "Zona 4 - IT Server Room & Core Network": "WA_GROUP_IT_SUPPORT",
}


async def send_wa_alert(area: str, device_type: str, name: str, ip_address: str, severity: str) -> None:
    """Send structured WhatsApp alert via Fonnte API or console fallback."""
    fonnte_token = os.getenv("FONNTE_TOKEN", "").strip()

    # Determine target WA groups
    target_groups = []
    env_var_name = AREA_WA_GROUP_MAP.get(area)
    if env_var_name:
        primary_group = os.getenv(env_var_name, "").strip()
        if primary_group:
            target_groups.append(primary_group)

    if severity.upper() == "DISASTER":
        manager_group = os.getenv("WA_GROUP_MANAGER_DEPOT", "").strip()
        if manager_group and manager_group not in target_groups:
            target_groups.append(manager_group)

    # Format WIB timestamp (UTC+7)
    wib_tz = timezone(timedelta(hours=7))
    timestamp_wib = datetime.now(wib_tz).strftime("%Y-%m-%d %H:%M:%S WIB")

    message = (
        "🚨 *[PERTAMINA NETSHIELD] ALERT INSIDEN FT PENGAPON*\n"
        f"📍 *Lokasi:* {area}\n"
        f"🏷️ *Perangkat:* {device_type} - {name}\n"
        f"🌐 *IP Address:* `{ip_address}`\n"
        "🔴 *Status:* DOWN (Request Timeout)\n"
        f"⚠️ *Severity:* {severity}\n"
        f"⏰ *Waktu:* {timestamp_wib}\n"
        "🔗 *Dashboard NOC:* http://192.168.150.117:5000/noc"
    )

    if not target_groups:
        logger.warning("No target WA group configured for area '%s' / severity '%s'", area, severity)
        target_groups = ["DEFAULT_LOG_ONLY"]

    for group_target in target_groups:
        if not fonnte_token or group_target == "DEFAULT_LOG_ONLY":
            # Fallback console logging for local testing
            log_output = (
                "\n" + "=" * 50 + "\n"
                f"[CONSOLE WA ALERT FALLBACK] Target Group: {group_target}\n"
                f"{message}\n"
                + "=" * 50 + "\n"
            )
            try:
                print(log_output)
            except UnicodeEncodeError:
                # Handle Windows console cp1252 encoding gracefully
                if hasattr(sys.stdout, "buffer"):
                    sys.stdout.buffer.write(log_output.encode("utf-8"))
                    sys.stdout.buffer.flush()
                else:
                    print(log_output.encode("ascii", errors="replace").decode("ascii"))

        else:
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    response = await client.post(
                        FONNTE_API_URL,
                        headers={"Authorization": fonnte_token},
                        data={
                            "target": group_target,
                            "message": message
                        }
                    )
                    logger.info(
                        "Fonnte WA Alert sent to %s (Status: %d, Response: %s)",
                        group_target,
                        response.status_code,
                        response.text
                    )
            except Exception as e:
                logger.error("Failed to send WA alert via Fonnte to %s: %s", group_target, e)
