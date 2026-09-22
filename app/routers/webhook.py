import re
import sys
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

# Flexible ACK Regex Pattern (Case-Insensitive):
# Matches "ACK 73 Sedang penanganan di lokasi", "ack#73 OTW", "ack 73", "ACK #73: Catatan", etc.
ACK_REGEX = re.compile(r"(?i)^\s*(?:ACK|ack)[#\s]*(\d+)(?:[\s:]+(.*))?$", re.DOTALL)


async def _extract_payload_data(
    request: Request,
    form_sender: Optional[str],
    form_message: Optional[str],
    form_name: Optional[str]
) -> tuple[str, str, str, Dict[str, Any]]:
    """
    Ekstrak sender, message, name, dan raw payload dari JSON payload, Form-Data, maupun Query Params Fonnte.
    """
    raw_payload: Dict[str, Any] = {}

    # 1. Parse JSON Body
    if request.headers.get("content-type", "").startswith("application/json"):
        try:
            parsed = await request.json()
            if isinstance(parsed, dict):
                raw_payload.update(parsed)
                if isinstance(parsed.get("data"), dict):
                    raw_payload.update(parsed["data"])
        except Exception:
            pass

    # 2. Parse Form Data
    try:
        form = await request.form()
        form_data = dict(form)
        if form_data:
            raw_payload.update(form_data)
    except Exception:
        pass

    # 3. Parse Query Params
    try:
        query = dict(request.query_params)
        if query:
            raw_payload.update(query)
    except Exception:
        pass

    def _to_str(val: Any) -> str:
        if isinstance(val, str):
            return val.strip()
        return ""

    s_form = _to_str(form_sender)
    m_form = _to_str(form_message)
    n_form = _to_str(form_name)

    sender_val = (
        s_form
        or _to_str(raw_payload.get("sender"))
        or _to_str(raw_payload.get("from"))
        or _to_str(raw_payload.get("phone"))
        or _to_str(raw_payload.get("member"))
        or _to_str(raw_payload.get("target"))
    )

    message_val = (
        m_form
        or _to_str(raw_payload.get("message"))
        or _to_str(raw_payload.get("text"))
        or _to_str(raw_payload.get("body"))
        or _to_str(raw_payload.get("pesan"))
    )

    name_val = (
        n_form
        or _to_str(raw_payload.get("name"))
        or _to_str(raw_payload.get("pushName"))
        or _to_str(raw_payload.get("contact"))
        or _to_str(raw_payload.get("username"))
    )

    return sender_val, message_val, name_val, raw_payload


@router.get("/whatsapp")
@router.get("/whatsapp/fonnte")
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
        # 1. Parsing & Logging Payload Fonnte di baris pertama
        sender_val, message_val, name_val, raw_payload = await _extract_payload_data(request, sender, message, name)
        
        log_payload_str = raw_payload if raw_payload else f"sender={sender_val}, message={message_val}, name={name_val}"
        print(f"[WEBHOOK INCOMING] Payload: {log_payload_str}")
        logger.info(
            "[WA WEBHOOK] Pesan masuk dari '%s' (%s): '%s'",
            name_val or "Unknown",
            sender_val or "No Number",
            message_val
        )

        if not message_val:
            print("[WEBHOOK IGNORED] Pesan kosong (empty message).")
            return {"status": "ok"}

        # 2. Robust Regex Parsing untuk Perintah ACK
        match = ACK_REGEX.search(message_val)
        if not match:
            print(f"[WEBHOOK REJECT] Pesan '{message_val}' tidak memenuhi pola/format ACK insiden.")
            logger.info("[WA WEBHOOK] Pesan '%s' tidak memenuhi format ACK insiden. Abaikan.", message_val)
            return {"status": "ok"}

        incident_id_str, raw_note = match.groups()
        try:
            incident_id = int(incident_id_str)
        except ValueError:
            print(f"[WEBHOOK REJECT] ID insiden '{incident_id_str}' tidak valid.")
            return {"status": "ok"}

        if raw_note:
            cleaned_note = raw_note.strip().lstrip(":").strip()
            note = cleaned_note if cleaned_note else "Dikonfirmasi via WhatsApp"
        else:
            note = "Dikonfirmasi via WhatsApp"

        print(f"[WEBHOOK ACK MATCH] ID Insiden: #{incident_id}, Catatan: '{note}', Sender: '{sender_val}' ({name_val})")

        # 3. Validasi Tiket Insiden & Mutasi Database
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
                print(f"[WEBHOOK REJECT] Tiket insiden #{incident_id} tidak ditemukan di database.")
                logger.warning("[WA WEBHOOK KASUS A] Insiden #%d tidak ditemukan.", incident_id)
                if sender_val:
                    try:
                        await send_wa_reply(sender_val, reply_a)
                    except Exception as err:
                        print(f"[WEBHOOK REPLY ERROR] Gagal mengirimi balasan ke {sender_val}: {err}")
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
                print(f"[WEBHOOK REJECT] Insiden #{incident_id} sudah di-ACK sebelumnya oleh {ack_by_str}.")
                logger.info("[WA WEBHOOK KASUS B] Insiden #%d sudah di-ACK oleh %s.", incident_id, ack_by_str)
                if sender_val:
                    try:
                        await send_wa_reply(sender_val, reply_b)
                    except Exception as err:
                        print(f"[WEBHOOK REPLY ERROR] Gagal mengirimi balasan ke {sender_val}: {err}")
                return {"status": "ok"}

            # Kasus C: Insiden sudah RESOLVED (sembuh)
            if incident.status == "RESOLVED":
                reply_c = f"ℹ️ Insiden #{incident_id} sudah terselesaikan (RESOLVED)."
                print(f"[WEBHOOK REJECT] Insiden #{incident_id} sudah terselesaikan (RESOLVED).")
                logger.info("[WA WEBHOOK KASUS C] Insiden #%d sudah RESOLVED.", incident_id)
                if sender_val:
                    try:
                        await send_wa_reply(sender_val, reply_c)
                    except Exception as err:
                        print(f"[WEBHOOK REPLY ERROR] Gagal mengirimi balasan ke {sender_val}: {err}")
                return {"status": "ok"}

            # Kasus D: Valid ACK -> Mutasi Record
            wib_tz = timezone(timedelta(hours=7))
            now_wib = datetime.now(wib_tz).replace(tzinfo=None)

            incident.status = "ACKNOWLEDGED"
            incident.is_acknowledged = True
            incident.acknowledged_by = acknowledged_by
            incident.acknowledged_at = now_wib
            incident.ack_by = acknowledged_by
            incident.ack_at = now_wib
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

            print(f"[WEBHOOK SUCCESS] Insiden #{incident_id} berhasil di-ACK oleh '{acknowledged_by}'. Catatan: '{note}'")
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
                print(f"[WEBHOOK REPLY] Mengirimkan balasan konfirmasi WA ke {sender_val}...")
                try:
                    await send_wa_reply(sender_val, confirm_msg)
                except Exception as err:
                    print(f"[WEBHOOK REPLY ERROR] Gagal mengirimi konfirmasi WA ke {sender_val}: {err}")
            else:
                print("[WEBHOOK WARNING] sender_val kosong, tidak ada nomor untuk membalas WA.")

            return {"status": "ok"}

        finally:
            db.close()

    except Exception as exc:
        print(f"[WEBHOOK ERROR] Error tidak terduga: {exc}")
        logger.error("[WA WEBHOOK ERROR] Error tidak terduga: %s", exc, exc_info=True)
        return {"status": "ok"}
