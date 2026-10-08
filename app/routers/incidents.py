"""
app/routers/incidents.py
========================
Router Incident Operations Center (IOC) & Manajemen Tiket Insiden Pertamina NetShield.
"""

import os
import csv
import io
from typing import List, Optional
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Depends, HTTPException, status, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, joinedload
from pydantic import BaseModel

from app.database import get_db
from app.models import Device, IncidentLog, AuditLog

# Setup Jinja2 Templates Directory
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEMPLATES_DIR = os.path.join(BASE_DIR, "app", "templates")
if not os.path.exists(TEMPLATES_DIR):
    TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")

templates = Jinja2Templates(directory=TEMPLATES_DIR)

# Main router with prefix="/incidents"
router = APIRouter(prefix="/incidents", tags=["Incident Operations Center"])

# Secondary API router for backward compatibility with AJAX/Prometheus polling
api_router = APIRouter(prefix="/api/incidents", tags=["Incidents API"])
v1_router = APIRouter(prefix="/api/v1/incidents", tags=["Incidents API V1"])


class AckPayload(BaseModel):
    ack_by: str
    ack_message: Optional[str] = None


# =============================================================================
# WEB INTERFACE ROUTE HANDLERS
# =============================================================================

@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def incidents_page(request: Request, db: Session = Depends(get_db)):
    """
    GET /incidents: Render Incident Operations Center (incidents.html)
    Menampilkan daftar seluruh insiden aktif & historis terurut dari yang terbaru.
    """
    incidents = (
        db.query(IncidentLog)
        .options(joinedload(IncidentLog.device))
        .order_by(IncidentLog.started_at.desc())
        .all()
    )
    current_user = request.session.get("user", "Operator On-Duty") if hasattr(request, "session") and request.session else "Operator On-Duty"

    return templates.TemplateResponse(
        request=request,
        name="incidents.html",
        context={
            "request": request,
            "incidents": incidents,
            "user": current_user,
            "active_page": "incidents",
            "app_name": os.getenv("APP_NAME", "Pertamina NetShield")
        }
    )


@router.post("/{incident_id}/claim")
def claim_incident(
    incident_id: int,
    request: Request,
    db: Session = Depends(get_db)
):
    """
    POST /incidents/{incident_id}/claim:
    - Mengambil nama operator dari session (default "Operator On-Duty").
    - Mengubah assigned_to = current_user dan status = IN_PROGRESS jika sebelumnya NEW atau ACKNOWLEDGED.
    - Redirect ke /incidents.
    """
    current_user = request.session.get("user", "Operator On-Duty") if hasattr(request, "session") and request.session else "Operator On-Duty"
    
    incident = db.query(IncidentLog).filter(IncidentLog.id == incident_id).first()
    if not incident:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tiket insiden tidak ditemukan.")

    if incident.status in ("NEW", "ACKNOWLEDGED"):
        now_utc = datetime.utcnow()
        incident.assigned_to = current_user
        incident.ack_by = current_user
        incident.status = "IN_PROGRESS"

        if not incident.acknowledged_at:
            incident.acknowledged_at = now_utc
            if incident.started_at:
                incident.tta_seconds = (now_utc - incident.started_at).total_seconds()

        # Audit Log
        audit = AuditLog(
            user_name=current_user,
            action=f"Claimed Incident ID #{incident_id} (Status: IN_PROGRESS)",
            ip_address=request.client.host if request.client else "127.0.0.1",
            timestamp=datetime.utcnow()
        )
        db.add(audit)
        db.commit()

    return RedirectResponse(url="/incidents", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/{incident_id}/resolve")
def resolve_incident(
    incident_id: int,
    request: Request,
    notes: Optional[str] = Form(None),
    db: Session = Depends(get_db)
):
    """
    POST /incidents/{incident_id}/resolve:
    - Menerima form parameter notes (Catatan Perbaikan / RCA).
    - Mengubah status menjadi RESOLVED, mengisi resolved_at, dan menghitung ttr_seconds.
    - Memeriksa jika perangkat tidak memiliki tiket aktif lain; jika bersih, set status perangkat menjadi NORMAL.
    - Redirect ke /incidents.
    """
    current_user = request.session.get("user", "Operator On-Duty") if hasattr(request, "session") and request.session else "Operator On-Duty"

    incident = db.query(IncidentLog).filter(IncidentLog.id == incident_id).first()
    if not incident:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tiket insiden tidak ditemukan.")

    now_utc = datetime.utcnow()
    incident.status = "RESOLVED"
    incident.resolution_notes = notes
    incident.resolved_at = now_utc

    if incident.started_at:
        incident.ttr_seconds = (now_utc - incident.started_at).total_seconds()

    # Cek tiket aktif lain pada perangkat yang sama
    active_incidents_count = db.query(IncidentLog).filter(
        IncidentLog.device_id == incident.device_id,
        IncidentLog.status != "RESOLVED",
        IncidentLog.id != incident.id
    ).count()

    if active_incidents_count == 0:
        device = db.query(Device).filter(Device.id == incident.device_id).first()
        if device:
            device.status = "NORMAL"

    # Audit Log
    audit = AuditLog(
        user_name=current_user,
        action=f"Resolved Incident ID #{incident_id} (RCA: {notes or 'N/A'})",
        ip_address=request.client.host if request.client else "127.0.0.1",
        timestamp=datetime.utcnow()
    )
    db.add(audit)
    db.commit()

    return RedirectResponse(url="/incidents", status_code=status.HTTP_303_SEE_OTHER)


# =============================================================================
# BACKWARD COMPATIBLE JSON API ENDPOINTS & CSV EXPORT
# =============================================================================

@api_router.get("")
def get_incidents_json(db: Session = Depends(get_db)):
    """API List all incidents."""
    return db.query(IncidentLog).options(joinedload(IncidentLog.device)).order_by(IncidentLog.started_at.desc()).all()


@api_router.get("/active")
@v1_router.get("/active")
def get_active_incidents_json(db: Session = Depends(get_db)):
    """API List active incidents for status polling."""
    incidents = (
        db.query(IncidentLog)
        .options(joinedload(IncidentLog.device))
        .filter(IncidentLog.status != "RESOLVED")
        .order_by(IncidentLog.started_at.desc())
        .all()
    )

    result = []
    wib_tz = timezone(timedelta(hours=7))

    for inc in incidents:
        dev_name = inc.device.name if inc.device else "N/A"
        ip_addr = inc.device.ip_address if inc.device else "N/A"
        loc_name = inc.device.location_name if inc.device else "N/A"

        ack_at_formatted = None
        if inc.acknowledged_at:
            ack_dt = inc.acknowledged_at.replace(tzinfo=wib_tz) if inc.acknowledged_at.tzinfo is None else inc.acknowledged_at.astimezone(wib_tz)
            ack_at_formatted = ack_dt.strftime("%H:%M WIB")

        result.append({
            "id": inc.id,
            "device_name": dev_name,
            "ip_address": ip_addr,
            "location_name": loc_name,
            "severity": inc.severity,
            "title": inc.title or ("Device DOWN" if inc.severity == "CRITICAL" else f"{inc.severity} Incident"),
            "latency_ms": inc.latency_ms,
            "status": inc.status,
            "assigned_to": inc.assigned_to,
            "acknowledged_at": ack_at_formatted,
            "resolution_notes": inc.resolution_notes or "",
            "started_at": inc.started_at.strftime("%Y-%m-%d %H:%M:%S") if inc.started_at else None,
        })

    return result


def format_duration(seconds: Optional[float]) -> str:
    if seconds is None or seconds < 0:
        return "-"
    tot = int(seconds)
    hours, remainder = divmod(tot, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours > 0:
        return f"{hours}h {minutes}m {secs}s"
    elif minutes > 0:
        return f"{minutes}m {secs}s"
    else:
        return f"{secs}s"


@api_router.get("/export-csv")
def export_incidents_csv(
    target_date: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """Stream incident logs as CSV file."""
    wib_tz = timezone(timedelta(hours=7))
    now_wib = datetime.now(wib_tz)

    if target_date:
        try:
            date_obj = datetime.strptime(target_date, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=400, detail="Format tanggal tidak valid. Gunakan YYYY-MM-DD.")
    else:
        date_obj = now_wib.date()

    report_date_str = date_obj.strftime("%Y-%m-%d")

    start_wib = datetime.combine(date_obj, datetime.min.time())
    end_wib = datetime.combine(date_obj, datetime.max.time())
    start_utc = start_wib - timedelta(hours=7)
    end_utc = end_wib - timedelta(hours=7)

    incidents = (
        db.query(IncidentLog)
        .options(joinedload(IncidentLog.device))
        .filter(IncidentLog.started_at >= start_utc, IncidentLog.started_at <= end_utc)
        .order_by(IncidentLog.started_at.desc())
        .all()
    )

    def iter_csv():
        output = io.StringIO()
        writer = csv.writer(output)
        yield "\ufeff"  # UTF-8 BOM

        writer.writerow([
            "ID Insiden",
            "Lokasi Operasional",
            "Nama Perangkat",
            "IP Address",
            "Severity",
            "Latency (ms)",
            "Status",
            "Waktu Mulai (WIB)",
            "Operator (Assignee)",
            "Waktu ACK (WIB)",
            "SLA ACK (TTA)",
            "Waktu Selesai (WIB)",
            "SLA Resolve (TTR)",
            "Catatan RCA"
        ])
        yield output.getvalue()
        output.seek(0)
        output.truncate(0)

        for item in incidents:
            dev_name = item.device.name if item.device else "N/A"
            ip_address = item.device.ip_address if item.device else "N/A"
            loc_name = item.device.location_name if item.device else "N/A"

            started_wib_dt = item.started_at + timedelta(hours=7) if item.started_at else None
            started_wib_str = started_wib_dt.strftime("%Y-%m-%d %H:%M:%S") if started_wib_dt else "-"

            ack_wib_dt = item.acknowledged_at + timedelta(hours=7) if item.acknowledged_at else None
            ack_wib_str = ack_wib_dt.strftime("%Y-%m-%d %H:%M:%S") if ack_wib_dt else "-"

            resolved_wib_dt = item.resolved_at + timedelta(hours=7) if item.resolved_at else None
            resolved_wib_str = resolved_wib_dt.strftime("%Y-%m-%d %H:%M:%S") if resolved_wib_dt else "-"

            tta_str = format_duration(item.tta_seconds)
            ttr_str = format_duration(item.ttr_seconds)
            latency_str = f"{item.latency_ms} ms" if item.latency_ms is not None else "-"

            writer.writerow([
                f"#INC-{item.id}",
                loc_name,
                dev_name,
                ip_address,
                item.severity,
                latency_str,
                item.status,
                started_wib_str,
                item.assigned_to or "-",
                ack_wib_str,
                tta_str,
                resolved_wib_str,
                ttr_str,
                item.resolution_notes or "-"
            ])
            yield output.getvalue()
            output.seek(0)
            output.truncate(0)

    filename = f"Pertamina_NetShield_Incidents_{report_date_str}.csv"
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    return StreamingResponse(iter_csv(), media_type="text/csv; charset=utf-8", headers=headers)
