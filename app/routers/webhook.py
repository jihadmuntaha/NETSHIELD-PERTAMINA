import re
import asyncio
import logging
from typing import Optional, Dict, Any
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends, HTTPException, status, Request, Form, Body
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.models.monitoring import IncidentLog, AuditLog
from app.services.notifier import send_wa_reply

logger = logging.getLogger("netshield_webhook")

router = APIRouter(prefix="/api/v1/webhook", tags=["Webhook"])

# Regex pattern to identify ACK replies:
# Matches: "ACK 104 Sedang cek switch", "ack#104 OTW lokasi", "ACK 104", "ACK#104: Penanganan"
ACK_REGEX = re.compile(r'^\s*ACK[#\s:]*(\d+)(?:\s+(.*))?$', re.IGNORECASE | re.DOTALL)


class WhatsAppWebhookPayload(BaseModel):
    sender: Optional[str] = Field(None, description="Nomor WhatsApp pengirim")
    message: Optional[str] = Field(None, description="Isi pesan dari pengirim")
    name: Optional[str] = Field(None, description="Nama kontak pengirim jika tersedia")
    url: Optional[str] = None
    filename: Optional[str] = None


def _extract_webhook_data(
    payload_body: Optional[Dict[str, Any]],
    form_sender: Optional[str],
    form_message: Optional[str],
    form_name: Optional[str]
) -> tuple[str, str, str]:
    """Helper untuk mengekstrak sender, message, name dari Form Data atau JSON payload Fonnte."""
    sender = form_sender or ""
    message = form_message or ""
    name = form_name or ""

    if payload_body and isinstance(payload_body, dict):
        sender = sender or payload_body.get("sender") or payload_body.get("from") or payload_body.get("phone") or ""
        message = message or payload_body.get("message") or payload_body.get("text") or payload_body.get("body") or ""
        name = name or payload_body.get("name") or payload_body.get("pushName") or payload_body.get("contact") or ""

    return str(sender).strip(), str(message).strip(), str(name).strip()


@router.post("/whatsapp")
@router.post("/whatsapp/fonnte")
async def whatsapp_webhook(
    request: Request,
    sender: Optional[str] = Form(None),
    message: Optional[str] = Form(None),
    name: Optional[str] = Form(None),
    db: Session = Depends(get_db)
):
    """
    Webhook endpoint untuk menerima pesan WhatsApp masuk dari Fonnte Gateway.
    Memproses Problem Acknowledgment (ACK) insiden secara otomatis.
    """
    # 1. Parse payload (baik JSON maupun Form-Data Fonnte)
    json_body = None
    if request.headers.get("content-type", "").startswith("application/json"):
        try:
            json_body = await request.json()
        except Exception:
            json_body = None

    sender_val, message_val, name_val = _extract_webhook_data(json_body, sender, message, name)

    logger.info("[WA WEBHOOK] Pesan masuk dari '%s' (%s): '%s'", name_val or "Unknown", sender_val or "No Number", message_val)

    if not message_val:
        return {
            "status": "ignored",
            "reason": "Pesan kosong",
            "timestamp": datetime.utcnow().isoformat()
        }

    # 2. Logika Parsing Pesan & Validasi Format ACK
    match = ACK_REGEX.match(message_val)
    if not match:
        logger.info("[WA WEBHOOK] Pesan tidak memenuhi format ACK insiden. Mengabaikan pesan.")
        return {
            "status": "ignored",
            "reason": "Pesan bukan format ACK (Format valid: ACK <incident_id> <catatan>)",
            "received_message": message_val
        }

    incident_id_str, catatan = match.groups()
    try:
        incident_id = int(incident_id_str)
    except ValueError:
        return {"status": "error", "message": f"ID insiden tidak valid: {incident_id_str}"}

    catatan = (catatan or "").strip()

    # 3. Cari Insiden di Database
    incident = (
        db.query(IncidentLog)
        .options(joinedload(IncidentLog.service))
        .filter(IncidentLog.id == incident_id)
        .first()
    )

    # Menentukan Nama Teknisi / PIC
    pic_name = name_val.strip() if name_val and name_val.strip() else f"Teknisi ({sender_val or 'WA'})"

    wib_tz = timezone(timedelta(hours=7))
    now_wib = datetime.now(wib_tz).replace(tzinfo=None)

    # 4. Handle Insiden Tidak Ditemukan
    if not incident:
        error_msg = f"❌ Gagal ACK: Insiden #{incident_id} tidak ditemukan di database NetShield."
        logger.warning("[WA WEBHOOK ACK FAILED] %s", error_msg)

        if sender_val:
            asyncio.create_task(send_wa_reply(sender_val, error_msg))

        return {
            "status": "not_found",
            "message": f"Insiden #{incident_id} tidak ditemukan.",
            "incident_id": incident_id
        }

    device_name = incident.service.name if incident.service else "Perangkat"

    # 5. Handle Insiden Sudah Di-ACK Sebelumnya
    if incident.is_acknowledged:
        existing_pic = incident.acknowledged_by or incident.ack_by or "Teknisi"
        already_ack_msg = (
            f"⚠️ Insiden #{incident_id} ({device_name}) sudah di-ACK sebelumnya!\n"
            f"PIC: {existing_pic}\n"
            f"Status di Dashboard NOC sudah ACKNOWLEDGED."
        )
        logger.info("[WA WEBHOOK ACK DUPLICATE] Insiden #%d sudah di-ACK oleh %s", incident_id, existing_pic)

        if sender_val:
            asyncio.create_task(send_wa_reply(sender_val, already_ack_msg))

        return {
            "status": "already_acknowledged",
            "message": f"Insiden #{incident_id} sudah di-ACK sebelumnya.",
            "acknowledged_by": existing_pic
        }

    # 6. Update Insiden ke Status ACKNOWLEDGED
    incident.is_acknowledged = True
    incident.acknowledged_by = pic_name
    incident.acknowledged_at = now_wib
    incident.ack_message = catatan

    client_ip = request.client.host if request.client else "127.0.0.1"
    audit = AuditLog(
        user_name=pic_name,
        action=f"ACK Incident #{incident_id} ({device_name}) via WhatsApp Webhook: {catatan or 'Tanpa catatan'}",
        ip_address=client_ip,
        timestamp=datetime.utcnow()
    )
    db.add(audit)

    db.commit()
    db.refresh(incident)

    logger.info("[WA WEBHOOK ACK SUCCESS] Insiden #%d (%s) berhasil di-ACK oleh %s", incident_id, device_name, pic_name)

    # 7. Balasan Konfirmasi Otomatis via Fonnte WA API
    confirmation_msg = (
        f"✅ *INSIDEN #{incident_id} BERHASIL DI-ACK!* ✅\n"
        f"*Perangkat:* {device_name}\n"
        f"*PIC:* {pic_name}\n"
        f"*Waktu ACK:* {now_wib.strftime('%d/%m/%Y %H:%M:%S WIB')}\n"
        f"*Catatan:* {catatan if catatan else '-'}\n\n"
        "_Status di Dashboard NOC telah diperbarui menjadi ACKNOWLEDGED._"
    )

    if sender_val:
        asyncio.create_task(send_wa_reply(sender_val, confirmation_msg))

    return {
        "status": "success",
        "message": f"Insiden #{incident_id} berhasil di-ACK oleh {pic_name}.",
        "incident_id": incident_id,
        "device_name": device_name,
        "acknowledged_by": pic_name,
        "acknowledged_at": now_wib.strftime("%Y-%m-%d %H:%M:%S WIB"),
        "catatan": catatan
    }
