import csv
import io
from typing import List, Optional
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Depends, HTTPException, status, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session, joinedload
from pydantic import BaseModel

from app.database import get_db
from app.models.monitoring import IncidentLog, AuditLog
from app.schemas.monitoring import IncidentResponse, IncidentAcknowledge

router = APIRouter(prefix="/api/incidents", tags=["Incidents"])


class AckPayload(BaseModel):
    ack_by: str
    ack_message: Optional[str] = None


@router.get("", response_model=List[IncidentResponse])
def get_incidents(db: Session = Depends(get_db)):
    """List all active and historical incident logs."""
    return db.query(IncidentLog).order_by(IncidentLog.created_at.desc()).all()


@router.post("/{incident_id}/ack", response_model=IncidentResponse)
def acknowledge_incident(
    incident_id: int,
    payload: AckPayload,
    request: Request,
    db: Session = Depends(get_db)
):
    """Acknowledge an incident log."""
    incident = db.query(IncidentLog).filter(IncidentLog.id == incident_id).first()
    if not incident:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Incident not found.")

    if incident.status == "ACKNOWLEDGED":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Incident is already acknowledged.")

    wib_tz = timezone(timedelta(hours=7))
    current_wib = datetime.now(wib_tz).replace(tzinfo=None)

    incident.status = "ACKNOWLEDGED"
    incident.ack_by = payload.ack_by
    incident.ack_message = payload.ack_message
    incident.ack_at = current_wib

    # Create Audit Log
    audit = AuditLog(
        user_name=payload.ack_by,
        action=f"Acknowledged Incident ID {incident_id}",
        ip_address=request.client.host if request.client else "127.0.0.1",
        timestamp=datetime.utcnow()
    )
    db.add(audit)

    db.commit()
    db.refresh(incident)
    return incident


@router.get("/export-csv")
def export_incidents_csv(db: Session = Depends(get_db)):
    """Stream all incident logs as a CSV file."""
    incidents = db.query(IncidentLog).options(joinedload(IncidentLog.service)).order_by(IncidentLog.created_at.desc()).all()

    def iter_csv():
        output = io.StringIO()
        writer = csv.writer(output)
        
        # Write CSV Header
        writer.writerow([
            "ID",
            "Service Name",
            "IP Address",
            "Area",
            "Severity",
            "Status",
            "Created At (UTC)",
            "Ack By",
            "Ack Message",
            "Ack At (WIB)"
        ])
        yield output.getvalue()
        output.seek(0)
        output.truncate(0)

        # Write Data Rows
        for item in incidents:
            service_name = item.service.name if item.service else "N/A"
            ip_address = item.service.ip_address if item.service else "N/A"
            area = item.service.area.value if item.service and hasattr(item.service.area, "value") else (item.service.area if item.service else "N/A")

            writer.writerow([
                item.id,
                service_name,
                ip_address,
                area,
                item.severity,
                item.status,
                item.created_at.strftime("%Y-%m-%d %H:%M:%S") if item.created_at else "",
                item.ack_by or "",
                item.ack_message or "",
                item.ack_at.strftime("%Y-%m-%d %H:%M:%S") if item.ack_at else ""
            ])
            yield output.getvalue()
            output.seek(0)
            output.truncate(0)

    headers = {
        "Content-Disposition": "attachment; filename=netshield_incident_logs.csv"
    }

    return StreamingResponse(iter_csv(), media_type="text/csv", headers=headers)
