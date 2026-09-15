import re
import asyncio
import logging
from typing import Optional, Dict, Any
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Request, Form
from sqlalchemy.orm import joinedload

from app.database import SessionLocal
from app.models.monitoring import IncidentLog, AuditLog
from app.services.notifier import send_wa_reply, format_ack_confirmation

logger = logging.getLogger("netshield_webhook")

router = APIRouter(prefix="/api/v1/webhook", tags=["Webhook"])

# Regex pattern untuk perintah ACK (case-insensitive & fleksibel):
# Format yang didukung: "ack 104 catatan", "ack#104 catatan", "ack 104", "ACK #104: catatan"
ACK_REGEX = re.compile(r"(?i)^\s*ack\s*#?\s*(\d+)[\s:]*(.*)?$", re.DOTALL)



async def _extract_payload_data(
    request: Request,
    form_sender: Optional[str],
    form_message: Optional[str],
    form_name: Optional[str]
) -> tuple[str, str, str]:
    """
    Ekstrak sender, message, dan name dari Form-Data maupun JSON payload Fonnte.
    """
    json_body: Dict[str, Any] = {}
    if request.headers.get("content-type", "").startswith("application/json"):
        try:
            parsed = await request.json()
            if isinstance(parsed, dict):
                json_body = parsed
        except Exception:
            json_body = {}

    form_data: Dict[str, Any] = {}
    try:
        form = await request.form()
        form_data = dict(form)
    except Exception:
        form_data = {}

    def _to_str(val: Any) -> str:
        return val if isinstance(val, str) else ""

    s_form = _to_str(form_sender)
    m_form = _to_str(form_message)
    n_form = _to_str(form_name)

    sender_val = (
        s_form
        or form_data.get("sender")
        or form_data.get("from")
        or form_data.get("phone")
        or json_body.get("sender")
        or json_body.get("from")
        or json_body.get("phone")
        or ""
    )

    message_val = (
        m_form
        or form_data.get("message")
        or form_data.get("text")
        or form_data.get("body")
        or json_body.get("message")
        or json_body.get("text")
        or json_body.get("body")
        or ""
    )

    name_val = (
        n_form
        or form_data.get("name")
        or form_data.get("pushName")
        or form_data.get("contact")
        or json_body.get("name")
        or json_body.get("pushName")
        or json_body.get("contact")
        or ""
    )

    return str(sender_val).strip(), str(message_val).strip(), str(name_val).strip()



@router.post("/whatsapp")
@router.post("/whatsapp/fonnte")
async def whatsapp_webhook(
    request: Request,
    sender: Optional[str] = Form(None),
    message: Optional[str] = Form(None),
    name: Optional[str] = Form(None)
):
    """
    Webhook endpoint untuk menerima pesan WhatsApp masuk dari Fonnte Gateway.
    Memproses Problem Acknowledgment (ACK) insiden secara otomatis.
    Selalu mengembalikan {"status": "ok"} HTTP 200 agar gateway Fonnte tidak melakukan retry.
    """
    try:
        # 1. Pengambilan Payload Fonnte (JSON & Form-Data)
        sender_val, message_val, name_val = await _extract_payload_data(request, sender, message, name)

        logger.info(
            "[WA WEBHOOK] Pesan masuk dari '%s' (%s): '%s'",
            name_val or "Unknown",
            sender_val or "No Number",
            message_val
        )

        if not message_val:
            return {"status": "ok"}

        # 2. Robust Regex Parsing untuk Perintah ACK
        match = ACK_REGEX.match(message_val)
        if not match:
            logger.info("[WA WEBHOOK] Pesan tidak memenuhi format ACK insiden. Abaikan.")
            return {"status": "ok"}

        incident_id_str, raw_note = match.groups()
        try:
            incident_id = int(incident_id_str)
        except ValueError:
            return {"status": "ok"}

        if raw_note:
            cleaned_note = raw_note.strip().lstrip(":").strip()
            note = cleaned_note if cleaned_note else "Dikonfirmasi via WhatsApp"
        else:
            note = "Dikonfirmasi via WhatsApp"

        # 3. Validasi & Mutasi Database (SessionLocal baru)
        db = SessionLocal()
        try:
            incident = (
                db.query(IncidentLog)
                .options(joinedload(IncidentLog.service))
                .filter(IncidentLog.id == incident_id)
                .first()
            )

            # Tentukan identitas PIC (name or sender)
            acknowledged_by = name_val if name_val else (sender_val if sender_val else "Teknisi WA")



            # Kasus A: Insiden tidak ditemukan
            if not incident:
                reply_a = f"❌ Insiden #{incident_id} tidak ditemukan di sistem."
                logger.warning("[WA WEBHOOK KASUS A] Insiden #%d tidak ditemukan.", incident_id)
                if sender_val:
                    asyncio.create_task(send_wa_reply(sender_val, reply_a))
                return {"status": "ok"}

            # Kasus B: Insiden sudah pernah di-ACK sebelumnya
            if incident.status == "ACKNOWLEDGED" or incident.is_acknowledged:
                ack_by_str = incident.acknowledged_by or incident.ack_by or "Teknisi"
                ack_at_dt = incident.acknowledged_at or incident.ack_at
                if ack_at_dt:
                    ack_at_str = ack_at_dt.strftime("%d/%m/%Y %H:%M:%S WIB")
                else:
                    ack_at_str = "sebelumnya"

                reply_b = f"⚠️ Insiden #{incident_id} sudah di-ACK sebelumnya oleh {ack_by_str} pada {ack_at_str}."
                logger.info("[WA WEBHOOK KASUS B] Insiden #%d sudah di-ACK oleh %s.", incident_id, ack_by_str)
                if sender_val:
                    asyncio.create_task(send_wa_reply(sender_val, reply_b))
                return {"status": "ok"}

            # Kasus C: Insiden sudah RESOLVED (sembuh)
            if incident.status == "RESOLVED":
                reply_c = f"ℹ️ Insiden #{incident_id} sudah terselesaikan (RESOLVED)."
                logger.info("[WA WEBHOOK KASUS C] Insiden #%d sudah RESOLVED.", incident_id)
                if sender_val:
                    asyncio.create_task(send_wa_reply(sender_val, reply_c))
                return {"status": "ok"}

            # Kasus D: Valid ACK -> Mutasi Record
            wib_tz = timezone(timedelta(hours=7))
            now_wib = datetime.now(wib_tz).replace(tzinfo=None)

            incident.status = "ACKNOWLEDGED"
            incident.is_acknowledged = True
            incident.acknowledged_by = acknowledged_by
            incident.acknowledged_at = now_wib
            incident.ack_message = note


            client_ip = request.client.host if request.client else "127.0.0.1"
            audit = AuditLog(
                user_name=acknowledged_by,
                action=f"ACK Incident #{incident_id} via WhatsApp Webhook: {note}",
                ip_address=client_ip,
                timestamp=datetime.utcnow()
            )
            db.add(audit)

            db.commit()
            db.refresh(incident)

            logger.info(
                "[WA WEBHOOK KASUS D SUCCESS] Insiden #%d berhasil di-ACK oleh %s.",
                incident_id,
                acknowledged_by
            )

            # 4. Pengiriman Pesan Konfirmasi Balik via Fonnte API
            device_name = incident.service.name if incident.service else "Perangkat Network"
            ip_address = incident.service.ip_address if incident.service else "0.0.0.0"
            zone_name = (
                incident.service.area.value
                if incident.service and hasattr(incident.service.area, "value")
                else str(incident.service.area if incident.service else "N/A")
            )
            acknowledged_at_formatted = now_wib.strftime("%d/%m/%Y %H:%M:%S")

            confirm_msg = format_ack_confirmation(
                incident_id=incident_id,
                device_name=device_name,
                ip_address=ip_address,
                zone_name=zone_name,
                acknowledged_by=acknowledged_by,
                acknowledged_at_formatted=acknowledged_at_formatted,
                ack_message=note
            )

            if sender_val:
                asyncio.create_task(send_wa_reply(sender_val, confirm_msg))

            return {"status": "ok"}

        finally:
            db.close()

    except Exception as exc:
        logger.error("[WA WEBHOOK ERROR] Error tidak terduga: %s", exc, exc_info=True)
        # 5. Proteksi Respon Endpoint (Selalu HTTP 200 status: ok)
        return {"status": "ok"}
