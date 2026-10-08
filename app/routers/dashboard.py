import os
from typing import Dict, List, Any
from fastapi import APIRouter, Depends, Request, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.models import Device, MonitoredService, IncidentLog, AuditLog, AreaZona

router = APIRouter(tags=["Dashboard"])

# Set templates directory path
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEMPLATES_DIR = os.path.join(BASE_DIR, "app", "templates")
if not os.path.exists(TEMPLATES_DIR):
    TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")

templates = Jinja2Templates(directory=TEMPLATES_DIR)


def calculate_zone_summary(services: List[MonitoredService]) -> List[Dict[str, Any]]:
    """Calculate summary statistics (Total, UP/NORMAL, DOWN/CRITICAL/WARNING, Maintenance) per zone/location."""
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
        area_str = service.area.value if hasattr(service.area, "value") and service.area else (service.location_name or "Zona Standard")
        if area_str not in summary_map:
            summary_map[area_str] = {
                "zone_name": area_str,
                "total": 0,
                "up": 0,
                "down": 0,
                "maintenance": 0
            }

        summary_map[area_str]["total"] += 1
        if getattr(service, "is_maintenance", False):
            summary_map[area_str]["maintenance"] += 1
        elif service.status in ("UP", "NORMAL"):
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
        .options(joinedload(IncidentLog.device))
        .order_by(IncidentLog.started_at.desc())
        .limit(10)
        .all()
    )

    zone_summary = calculate_zone_summary(services)
    user = request.session.get("user", "Operator On-Duty") if hasattr(request, "session") and request.session else "Operator On-Duty"

    return templates.TemplateResponse(
        request=request,
        name="noc.html",
        context={
            "services": services,
            "incidents": incidents,
            "zone_summary": zone_summary,
            "active_page": "noc",
            "user": user,
            "app_name": os.getenv("APP_NAME", "Pertamina NetShield")
        }
    )


@router.get("/topology", response_class=HTMLResponse)
def topology_view(request: Request, db: Session = Depends(get_db)):
    """Render Topology View web page."""
    services = db.query(MonitoredService).all()
    incidents = (
        db.query(IncidentLog)
        .options(joinedload(IncidentLog.device))
        .filter(IncidentLog.status != "RESOLVED")
        .all()
    )
    zone_summary = calculate_zone_summary(services)
    user = request.session.get("user", "Operator On-Duty") if hasattr(request, "session") and request.session else "Operator On-Duty"

    return templates.TemplateResponse(
        request=request,
        name="noc.html",
        context={
            "services": services,
            "incidents": incidents,
            "zone_summary": zone_summary,
            "active_page": "topology",
            "user": user,
            "app_name": os.getenv("APP_NAME", "Pertamina NetShield")
        }
    )


@router.get("/import-assets", response_class=HTMLResponse)
@router.get("/import-kmz", response_class=HTMLResponse)
def import_assets_view(request: Request, db: Session = Depends(get_db)):
    """Render Assets Import web page."""
    asset_logs = (
        db.query(AuditLog)
        .filter(AuditLog.action.like("%Imported%"))
        .order_by(AuditLog.timestamp.desc())
        .limit(50)
        .all()
    )
    user = request.session.get("user", "Operator On-Duty") if hasattr(request, "session") and request.session else "Operator On-Duty"

    return templates.TemplateResponse(
        request=request,
        name="import_assets.html",
        context={
            "asset_logs": asset_logs,
            "kmz_logs": asset_logs,
            "active_page": "import-assets",
            "user": user,
            "app_name": os.getenv("APP_NAME", "Pertamina NetShield")
        }
    )


@router.get("/devices", response_class=HTMLResponse)
def devices_inventory_view(request: Request, db: Session = Depends(get_db)):
    """Render Device Inventory Management web page."""
    services = db.query(MonitoredService).all()
    user = request.session.get("user", "Operator On-Duty") if hasattr(request, "session") and request.session else "Operator On-Duty"

    return templates.TemplateResponse(
        request=request,
        name="devices.html",
        context={
            "services": services,
            "active_page": "devices",
            "user": user,
            "app_name": os.getenv("APP_NAME", "Pertamina NetShield")
        }
    )


@router.get("/audit", response_class=HTMLResponse)
def audit_view(request: Request, db: Session = Depends(get_db)):
    """Render Audit Logs & System Configuration web page."""
    audit_logs = db.query(AuditLog).order_by(AuditLog.timestamp.desc()).limit(100).all()
    user = request.session.get("user", "Operator On-Duty") if hasattr(request, "session") and request.session else "Operator On-Duty"

    return templates.TemplateResponse(
        request=request,
        name="audit.html",
        context={
            "audit_logs": audit_logs,
            "active_page": "audit",
            "user": user,
            "app_name": os.getenv("APP_NAME", "Pertamina NetShield")
        }
    )


@router.get("/api/dashboard-summary")
def get_dashboard_summary(db: Session = Depends(get_db)):
    """API Endpoint returning real-time status summary for AJAX polling."""
    services = db.query(MonitoredService).all()
    incidents = (
        db.query(IncidentLog)
        .options(joinedload(IncidentLog.device))
        .order_by(IncidentLog.started_at.desc())
        .limit(10)
        .all()
    )

    total_services = len(services)
    up_count = sum(1 for s in services if s.status in ("UP", "NORMAL") and not s.is_maintenance)
    down_count = sum(1 for s in services if s.status in ("DOWN", "CRITICAL", "WARNING") and not s.is_maintenance)
    maintenance_count = sum(1 for s in services if s.is_maintenance)

    zone_summary = calculate_zone_summary(services)

    services_data = [
        {
            "id": s.id,
            "name": s.name,
            "ip_address": s.ip_address,
            "location_name": getattr(s, "location_name", "Regional HQ Semarang"),
            "site_type": getattr(s, "site_type", "FUEL_TERMINAL"),
            "level": getattr(s, "level", 0),
            "area": s.area.value if hasattr(s.area, "value") and s.area else str(s.area),
            "device_type": s.device_type.value if hasattr(s.device_type, "value") and s.device_type else str(s.device_type),
            "status": s.status,
            "response_time_ms": s.response_time_ms,
            "is_maintenance": s.is_maintenance,
            "last_check": s.last_check.isoformat() if s.last_check else None,
        }
        for s in services
    ]

    incidents_data = [
        {
            "id": inc.id,
            "device_id": inc.device_id,
            "device_name": inc.device.name if inc.device else "N/A",
            "ip_address": inc.device.ip_address if inc.device else "N/A",
            "location_name": inc.device.location_name if inc.device else "N/A",
            "severity": inc.severity,
            "title": inc.title or ("Device DOWN" if inc.severity == "CRITICAL" else f"{inc.severity} Incident"),
            "latency_ms": inc.latency_ms,
            "status": inc.status,
            "assigned_to": inc.assigned_to,
            "resolution_notes": inc.resolution_notes,
            "started_at": inc.started_at.isoformat() if inc.started_at else None,
            "acknowledged_at": inc.acknowledged_at.isoformat() if inc.acknowledged_at else None,
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
