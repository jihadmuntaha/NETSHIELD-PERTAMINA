"""
app/routers/incidents.py
========================
Router Incident Operations Center (IOC) & Manajemen Tiket Insiden Pertamina NetShield.
"""

import os
import csv
import io
from typing import List, Optional, Tuple
from datetime import datetime, date, timezone, timedelta
from fastapi import APIRouter, Depends, HTTPException, status, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, joinedload
from pydantic import BaseModel

from app.database import get_db
from app.models import Device, IncidentLog, AuditLog
from app.core.timeutil import now_wib

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
# HELPER: LIFECYCLE DOWN -> UP, TRIGGER CAUSE & CSV SAFETY
# =============================================================================

MONTH_NAMES_ID = [
    "Januari", "Februari", "Maret", "April", "Mei", "Juni",
    "Juli", "Agustus", "September", "Oktober", "November", "Desember",
]

# Severity yang dibuat saat host tidak merespons (up=0).
# CRITICAL dibuat oleh checker/poller; HIGH & DISASTER dibuat saat server Prometheus mati.
_HOST_DOWN_SEVERITIES = ("CRITICAL", "HIGH", "DISASTER")

CSV_COLUMNS = [
    "Incident_ID",
    "Timestamp_Down",
    "Timestamp_Up",
    "Duration_Downtime_Sec",
    "Location",
    "Device_Name",
    "IP_Address",
    "Severity",
    "Trigger_Cause",
    "Status",
    "Handled_By",
    "TTA_Seconds",
    "TTR_Seconds",
    "Resolution_Notes",
]


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


def _format_metric(value: Optional[float]) -> str:
    """Format nilai metrik (mis. RTT) tanpa nol berlebih: 250.0 -> '250', 250.55 -> '250.55'."""
    if value is None:
        return "N/A"
    return f"{value:.2f}".rstrip("0").rstrip(".")


def trigger_cause(inc: IncidentLog) -> str:
    """
    Penyebab pemicu gangguan berdasarkan severity tiket:
      - CRITICAL -> "Host Unreachable (up=0)"
      - WARNING  -> "High Latency (RTT: {metric_value}ms)"
    HIGH / DISASTER (dibuat saat host/server monitoring tidak terjangkau) diperlakukan
    sama dengan CRITICAL karena juga dipicu oleh kondisi up=0.
    """
    severity = (inc.severity or "").upper()
    if severity == "WARNING":
        return f"High Latency (RTT: {_format_metric(inc.latency_ms)}ms)"
    if severity in _HOST_DOWN_SEVERITIES:
        return "Host Unreachable (up=0)"
    return inc.title or "Unknown"


def ttr_seconds_of(inc: IncidentLog) -> Optional[float]:
    """
    Durasi Down -> Up (TTR) dalam detik. Memakai kolom ttr_seconds jika valid;
    jika kosong / negatif (data lama), dihitung ulang dari resolved_at - started_at.
    """
    if inc.ttr_seconds is not None and inc.ttr_seconds >= 0:
        return inc.ttr_seconds
    if inc.resolved_at and inc.started_at:
        return max((inc.resolved_at - inc.started_at).total_seconds(), 0.0)
    return None


def tta_seconds_of(inc: IncidentLog) -> Optional[float]:
    """Time-To-Acknowledge dalam detik (fallback ke acknowledged_at - started_at)."""
    if inc.tta_seconds is not None and inc.tta_seconds >= 0:
        return inc.tta_seconds
    if inc.acknowledged_at and inc.started_at:
        return max((inc.acknowledged_at - inc.started_at).total_seconds(), 0.0)
    return None


def downtime_seconds(inc: IncidentLog, now: Optional[datetime] = None) -> Optional[float]:
    """
    Total durasi downtime. Untuk tiket yang sudah pulih = TTR; untuk tiket yang belum pulih
    dikembalikan durasi berjalan (sekarang - started_at).
    """
    ttr = ttr_seconds_of(inc)
    if ttr is not None:
        return ttr
    if inc.started_at and not inc.resolved_at and inc.status != "RESOLVED":
        return max(((now or now_wib()) - inc.started_at).total_seconds(), 0.0)
    return None


_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _csv_safe(value) -> str:
    """Cegah CSV/formula injection (mis. catatan RCA berawalan '=' saat dibuka di Excel)."""
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(_CSV_FORMULA_PREFIXES) else text


def _fmt_ts(dt: Optional[datetime]) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else ""


def _fmt_secs(value: Optional[float]):
    return "" if value is None else int(round(value))


def _parse_date_param(value: Optional[str], field: str) -> Optional[date]:
    if value is None or not value.strip():
        return None
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Format {field} tidak valid. Gunakan YYYY-MM-DD.",
        )


def _parse_int_param(value: Optional[str], field: str, low: int, high: int) -> Optional[int]:
    if value is None or not str(value).strip():
        return None
    try:
        number = int(str(value).strip())
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Parameter {field} harus berupa angka.",
        )
    if not low <= number <= high:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Parameter {field} harus di antara {low} dan {high}.",
        )
    return number


def resolve_report_range(
    start_date: Optional[str],
    end_date: Optional[str],
    month: Optional[str],
    year: Optional[str],
) -> Tuple[Optional[datetime], Optional[datetime]]:
    """
    Terjemahkan parameter filter menjadi rentang [start, end) pada kolom started_at.

    Prioritas: `month` (+`year`, default tahun berjalan)  >  `start_date`/`end_date`  >  `year` saja.
    Semua parameter opsional; tanpa parameter -> seluruh insiden.
    """
    month_i = _parse_int_param(month, "month", 1, 12)
    year_i = _parse_int_param(year, "year", 2000, 2100)

    if month_i is not None:
        y = year_i or now_wib().year
        start = datetime(y, month_i, 1)
        end = datetime(y + 1, 1, 1) if month_i == 12 else datetime(y, month_i + 1, 1)
        return start, end

    start_d = _parse_date_param(start_date, "start_date")
    end_d = _parse_date_param(end_date, "end_date")

    if start_d or end_d:
        if start_d and end_d and start_d > end_d:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="start_date tidak boleh lebih besar dari end_date.",
            )
        start = datetime.combine(start_d, datetime.min.time()) if start_d else None
        end = datetime.combine(end_d + timedelta(days=1), datetime.min.time()) if end_d else None
        return start, end

    if year_i is not None:
        return datetime(year_i, 1, 1), datetime(year_i + 1, 1, 1)

    return None, None


# Daftarkan helper ke Jinja2 agar tabel Incident Center memakai logika yang sama dengan CSV.
templates.env.globals.update(
    trigger_cause=trigger_cause,
    downtime_seconds=downtime_seconds,
    format_duration=format_duration,
)


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
    today = now_wib()

    return templates.TemplateResponse(
        request=request,
        name="incidents.html",
        context={
            "request": request,
            "incidents": incidents,
            "user": current_user,
            "active_page": "incidents",
            "month_names": MONTH_NAMES_ID,
            "current_month": today.month,
            "current_year": today.year,
            "today": today.strftime("%Y-%m-%d"),
            "app_name": os.getenv("APP_NAME", "Pertamina NetShield")
        }
    )


@router.get("/export/csv")
def export_incidents_range_csv(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    month: Optional[str] = None,
    year: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """
    GET /incidents/export/csv — unduh laporan gangguan (Down -> Up) dalam format CSV.

    Query parameter (semua opsional; string kosong dianggap tidak diisi):
      - start_date, end_date : rentang hari (YYYY-MM-DD, inklusif).
      - month (1-12), year   : filter per bulan. `month` lebih diprioritaskan daripada rentang hari.
    Filter diterapkan pada waktu gangguan dimulai (started_at).
    """
    start_dt, end_dt = resolve_report_range(start_date, end_date, month, year)

    query = db.query(IncidentLog).options(joinedload(IncidentLog.device))
    if start_dt is not None:
        query = query.filter(IncidentLog.started_at >= start_dt)
    if end_dt is not None:
        query = query.filter(IncidentLog.started_at < end_dt)
    incidents = query.order_by(IncidentLog.started_at.asc(), IncidentLog.id.asc()).all()

    report_now = now_wib()

    def iter_csv():
        buffer = io.StringIO()
        writer = csv.writer(buffer)

        def drain() -> str:
            chunk = buffer.getvalue()
            buffer.seek(0)
            buffer.truncate(0)
            return chunk

        yield "\ufeff"  # UTF-8 BOM agar Excel membaca karakter dengan benar
        writer.writerow(CSV_COLUMNS)
        yield drain()

        for item in incidents:
            device = item.device
            handled_by = item.assigned_to or item.ack_by

            writer.writerow([
                f"INC-{item.id}",
                _fmt_ts(item.started_at),
                _fmt_ts(item.resolved_at) if item.resolved_at else "STILL DOWN",
                _fmt_secs(downtime_seconds(item, report_now)),
                _csv_safe(device.location_name if device else ""),
                _csv_safe(device.name if device else ""),
                _csv_safe(device.ip_address if device else ""),
                item.severity,
                trigger_cause(item),
                item.status,
                _csv_safe(handled_by),
                _fmt_secs(tta_seconds_of(item)),
                _fmt_secs(ttr_seconds_of(item)),
                _csv_safe(item.resolution_notes),
            ])
            yield drain()

    filename = f"NetShield_Incidents_Report_{report_now.strftime('%Y%m%d_%H%M%S')}.csv"
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    return StreamingResponse(iter_csv(), media_type="text/csv; charset=utf-8", headers=headers)


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
        # Timestamp insiden disimpan dalam WIB (naive) -> pakai now_wib agar TTA tidak bergeser 7 jam.
        now_ts = now_wib()
        incident.assigned_to = current_user
        incident.ack_by = current_user
        incident.status = "IN_PROGRESS"

        if not incident.acknowledged_at:
            incident.acknowledged_at = now_ts
            if incident.started_at:
                incident.tta_seconds = max((now_ts - incident.started_at).total_seconds(), 0.0)

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

    # Tiket yang sudah ditutup (manual / auto-recovery) tidak boleh ditimpa waktu pulihnya.
    if incident.status == "RESOLVED":
        return RedirectResponse(url="/incidents", status_code=status.HTTP_303_SEE_OTHER)

    now_ts = now_wib()
    incident.status = "RESOLVED"
    incident.resolution_notes = notes
    incident.resolved_at = now_ts
    if not incident.assigned_to:
        incident.assigned_to = current_user  # penanganan langsung tanpa klaim tetap tercatat

    if incident.started_at:
        incident.ttr_seconds = max((now_ts - incident.started_at).total_seconds(), 0.0)

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


@api_router.get("/export-csv")
def export_incidents_csv(
    target_date: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """Stream incident logs as CSV file."""
    wib_tz = timezone(timedelta(hours=7))
    now_wib_dt = datetime.now(wib_tz)

    if target_date:
        try:
            date_obj = datetime.strptime(target_date, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=400, detail="Format tanggal tidak valid. Gunakan YYYY-MM-DD.")
    else:
        date_obj = now_wib_dt.date()

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
