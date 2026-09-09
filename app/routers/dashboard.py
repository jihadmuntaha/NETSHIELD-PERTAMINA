import os
from typing import Dict, List, Any
from fastapi import APIRouter, Depends, Request, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.models.monitoring import MonitoredService, IncidentLog, AuditLog, AreaZona

router = APIRouter(tags=["Dashboard"])

# Set templates directory path
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEMPLATES_DIR = os.path.join(BASE_DIR, "app", "templates")
if not os.path.exists(TEMPLATES_DIR):
    TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")

templates = Jinja2Templates(directory=TEMPLATES_DIR)


def calculate_zone_summary(services: List[MonitoredService]) -> List[Dict[str, Any]]:
    """Calculate summary statistics (Total, UP, DOWN, Maintenance) for each AreaZona."""
    zones = [
        AreaZona.ZONA_1.value,
        AreaZona.ZONA_2.value,
        AreaZona.ZONA_3.value,
        AreaZona.ZONA_4.value,
    ]

    summary_map = {
        zone: {"zone_name": zone, "total": 0, "up": 0, "down": 0, "maintenance": 0}
        for zone in zones
    }

    for service in services:
        area_str = service.area.value if hasattr(service.area, "value") else str(service.area)
        if area_str not in summary_map:
            summary_map[area_str] = {
                "zone_name": area_str,
                "total": 0,
                "up": 0,
                "down": 0,
                "maintenance": 0
            }

        summary_map[area_str]["total"] += 1
        if service.is_maintenance:
            summary_map[area_str]["maintenance"] += 1
        elif service.status == "UP":
            summary_map[area_str]["up"] += 1
        else:
            summary_map[area_str]["down"] += 1

    return list(summary_map.values())


@router.get("/noc", response_class=HTMLResponse)
def noc_dashboard(request: Request, db: Session = Depends(get_db)):
    """Render NOC Dashboard web page."""
    services = db.query(MonitoredService).all()
    incidents = (
        db.query(IncidentLog)
        .options(joinedload(IncidentLog.service))
        .order_by(IncidentLog.created_at.desc())
        .limit(10)
        .all()
    )

    zone_summary = calculate_zone_summary(services)

    return templates.TemplateResponse(
        request=request,
        name="noc.html",
        context={
            "services": services,
            "incidents": incidents,
            "zone_summary": zone_summary,
            "active_page": "noc",
            "app_name": os.getenv("APP_NAME", "Pertamina NetShield")
        }
    )


@router.get("/topology", response_class=HTMLResponse)
def topology_view(request: Request, db: Session = Depends(get_db)):
    """Render Network Topology 3-Tier Hierarchy web page."""
    services = db.query(MonitoredService).all()
    return templates.TemplateResponse(
        request=request,
        name="topology.html",
        context={
            "services": services,
            "active_page": "topology",
            "app_name": os.getenv("APP_NAME", "Pertamina NetShield")
        }
    )


@router.get("/import-kmz", response_class=HTMLResponse)
def import_kmz_view(request: Request, db: Session = Depends(get_db)):
    """Render KMZ Asset Import web page."""
    kmz_logs = (
        db.query(AuditLog)
        .filter(AuditLog.action.like("%Imported KMZ%"))
        .order_by(AuditLog.timestamp.desc())
        .limit(50)
        .all()
    )
    return templates.TemplateResponse(
        request=request,
        name="import_kmz.html",
        context={
            "kmz_logs": kmz_logs,
            "active_page": "import-kmz",
            "app_name": os.getenv("APP_NAME", "Pertamina NetShield")
        }
    )


@router.get("/devices", response_class=HTMLResponse)
def devices_inventory_view(request: Request, db: Session = Depends(get_db)):
    """Render Device Inventory Management web page."""
    services = db.query(MonitoredService).all()
    return templates.TemplateResponse(
        request=request,
        name="devices.html",
        context={
            "services": services,
            "active_page": "devices",
            "app_name": os.getenv("APP_NAME", "Pertamina NetShield")
        }
    )


@router.get("/incidents", response_class=HTMLResponse)
def incidents_view(request: Request, db: Session = Depends(get_db)):
    """Render Incident Logs & Claim ACK web page."""
    incidents = (
        db.query(IncidentLog)
        .options(joinedload(IncidentLog.service))
        .order_by(IncidentLog.created_at.desc())
        .all()
    )
    return templates.TemplateResponse(
        request=request,
        name="incidents.html",
        context={
            "incidents": incidents,
            "active_page": "incidents",
            "app_name": os.getenv("APP_NAME", "Pertamina NetShield")
        }
    )


@router.get("/audit", response_class=HTMLResponse)
def audit_view(request: Request, db: Session = Depends(get_db)):
    """Render Audit Logs & System Configuration web page."""
    audit_logs = db.query(AuditLog).order_by(AuditLog.timestamp.desc()).limit(100).all()
    return templates.TemplateResponse(
        request=request,
        name="audit.html",
        context={
            "audit_logs": audit_logs,
            "active_page": "audit",
            "app_name": os.getenv("APP_NAME", "Pertamina NetShield")
        }
    )



@router.get("/api/dashboard-summary")
def get_dashboard_summary(db: Session = Depends(get_db)):
    """API Endpoint returning real-time status summary for AJAX polling."""
    services = db.query(MonitoredService).all()
    incidents = (
        db.query(IncidentLog)
        .options(joinedload(IncidentLog.service))
        .order_by(IncidentLog.created_at.desc())
        .limit(10)
        .all()
    )

    total_services = len(services)
    up_count = sum(1 for s in services if s.status == "UP" and not s.is_maintenance)
    down_count = sum(1 for s in services if s.status == "DOWN" and not s.is_maintenance)
    maintenance_count = sum(1 for s in services if s.is_maintenance)

    zone_summary = calculate_zone_summary(services)

    # Format services list for API response
    services_data = [
        {
            "id": s.id,
            "name": s.name,
            "ip_address": s.ip_address,
            "area": s.area.value if hasattr(s.area, "value") else str(s.area),
            "device_type": s.device_type.value if hasattr(s.device_type, "value") else str(s.device_type),
            "status": s.status,
            "response_time_ms": s.response_time_ms,
            "is_maintenance": s.is_maintenance,
            "last_check": s.last_check.isoformat() if s.last_check else None,
        }
        for s in services
    ]

    # Format recent incidents
    incidents_data = [
        {
            "id": inc.id,
            "service_id": inc.service_id,
            "service_name": inc.service.name if inc.service else "N/A",
            "ip_address": inc.service.ip_address if inc.service else "N/A",
            "severity": inc.severity,
            "status": inc.status,
            "ack_by": inc.ack_by,
            "ack_message": inc.ack_message,
            "created_at": inc.created_at.isoformat() if inc.created_at else None,
            "ack_at": inc.ack_at.isoformat() if inc.ack_at else None,
        }
        for inc in incidents
    ]

    return {
        "metrics": {
            "total_services": total_services,
            "up": up_count,
            "down": down_count,
            "maintenance": maintenance_count,
        },
        "zone_summary": zone_summary,
        "services": services_data,
        "recent_incidents": incidents_data,
    }
