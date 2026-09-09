import io
import re
import zipfile
import xml.etree.ElementTree as ET
from typing import List, Optional
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, status, Request, UploadFile, File
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.monitoring import MonitoredService, AuditLog, AreaZona, DeviceType
from app.schemas.monitoring import ServiceCreate, ServiceUpdate, ServiceResponse

router = APIRouter(prefix="/api/devices", tags=["Devices"])


def get_tag_name(elem: ET.Element) -> str:
    return elem.tag.split('}')[-1] if '}' in elem.tag else elem.tag


def find_first_text(parent: ET.Element, target_local_name: str) -> Optional[str]:
    for child in parent.iter():
        if get_tag_name(child).lower() == target_local_name.lower() and child.text:
            return child.text.strip()
    return None


def infer_area_zona(name: str, desc: str) -> AreaZona:
    combined = (name + " " + desc).lower()
    if "zona 1" in combined or "gate" in combined or "office" in combined or "pos 1" in combined:
        return AreaZona.ZONA_1
    elif "zona 3" in combined or "filling" in combined or "shed" in combined or "loading" in combined or "bay" in combined or "island" in combined or "presisi" in combined:
        return AreaZona.ZONA_3
    elif "zona 4" in combined or "server" in combined or "core" in combined or "backbone" in combined or "it room" in combined:
        return AreaZona.ZONA_4
    elif "zona 2" in combined or "tank" in combined or "tangki" in combined or "farm" in combined or "outpost" in combined:
        return AreaZona.ZONA_2
    return AreaZona.ZONA_2


def infer_device_type(name: str, desc: str) -> DeviceType:
    combined = (name + " " + desc).lower()
    if "router" in combined or "rt-" in combined or "gateway" in combined:
        return DeviceType.ROUTER
    elif "switch" in combined or "sw-" in combined or "hub" in combined:
        return DeviceType.SWITCH
    return DeviceType.CCTV


def generate_available_ip(area: AreaZona, existing_ips: set) -> str:
    subnet_map = {
        AreaZona.ZONA_1: 12,
        AreaZona.ZONA_2: 13,
        AreaZona.ZONA_3: 14,
        AreaZona.ZONA_4: 11,
    }
    octet3 = subnet_map.get(area, 13)
    for host in range(100, 255):
        ip_candidate = f"10.4.{octet3}.{host}"
        if ip_candidate not in existing_ips:
            existing_ips.add(ip_candidate)
            return ip_candidate
    for sub in range(20, 99):
        for host in range(1, 255):
            ip_candidate = f"10.4.{sub}.{host}"
            if ip_candidate not in existing_ips:
                existing_ips.add(ip_candidate)
                return ip_candidate
    return f"10.14.0.{len(existing_ips) + 1}"


@router.post("/upload-kmz", status_code=status.HTTP_201_CREATED)
async def upload_kmz(request: Request, file: UploadFile = File(...), db: Session = Depends(get_db)):
    """Upload and parse a Google Earth KMZ/KML file to import monitored services."""
    if not file.filename.lower().endswith(('.kmz', '.zip', '.kml')):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Format file tidak didukung. Harap unggah file ber-ekstensi .kmz atau .kml"
        )

    file_bytes = await file.read()
    kml_content = None

    if file.filename.lower().endswith('.kml'):
        kml_content = file_bytes
    else:
        try:
            with zipfile.ZipFile(io.BytesIO(file_bytes)) as z:
                kml_filename = None
                for filename in z.namelist():
                    if filename.lower().endswith('.kml'):
                        kml_filename = filename
                        if filename.lower() == 'doc.kml':
                            break
                if not kml_filename:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="File .kml tidak ditemukan di dalam arsip KMZ."
                    )
                kml_content = z.read(kml_filename)
        except zipfile.BadZipFile:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Arsip file .kmz rusak atau tidak valid."
            )

    try:
        root = ET.fromstring(kml_content)
    except ET.ParseError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Gagal memproses XML dari KML document: {str(e)}"
        )

    existing_services = db.query(MonitoredService).all()
    existing_ips = {s.ip_address for s in existing_services}

    imported_devices = []
    placemarks = [elem for elem in root.iter() if get_tag_name(elem).lower() == 'placemark']

    if not placemarks:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Tidak ada elemen <Placemark> yang ditemukan di file KMZ/KML."
        )

    ip_regex = re.compile(r'\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b')

    for placemark in placemarks:
        name = find_first_text(placemark, 'name') or f"Placemark Node {len(imported_devices) + 1}"
        desc = find_first_text(placemark, 'description') or ""
        coords = find_first_text(placemark, 'coordinates') or ""

        found_ips = ip_regex.findall(name + " " + desc)
        device_ip = None
        for candidate_ip in found_ips:
            if candidate_ip not in existing_ips:
                device_ip = candidate_ip
                existing_ips.add(device_ip)
                break

        area = infer_area_zona(name, desc)
        device_type = infer_device_type(name, desc)

        if not device_ip:
            device_ip = generate_available_ip(area, existing_ips)

        service = MonitoredService(
            name=name[:100],
            ip_address=device_ip,
            area=area,
            device_type=device_type,
            status="UP",
            response_time_ms=0.0,
            is_maintenance=False
        )
        db.add(service)
        imported_devices.append({
            "name": service.name,
            "ip_address": service.ip_address,
            "area": area.value if hasattr(area, "value") else str(area),
            "device_type": device_type.value if hasattr(device_type, "value") else str(device_type),
            "coordinates": coords
        })

    client_ip = request.client.host if request.client else "127.0.0.1"
    audit = AuditLog(
        user_name="SYSTEM",
        action=f"Imported KMZ file '{file.filename}': added {len(imported_devices)} placemark devices",
        ip_address=client_ip,
        timestamp=datetime.utcnow()
    )
    db.add(audit)

    db.commit()

    return {
        "status": "success",
        "message": f"Berhasil mengimpor {len(imported_devices)} perangkat dari '{file.filename}'.",
        "imported_count": len(imported_devices),
        "devices": imported_devices
    }



@router.get("", response_model=List[ServiceResponse])
def get_devices(db: Session = Depends(get_db)):
    """List all monitored devices."""
    return db.query(MonitoredService).all()


@router.post("", response_model=ServiceResponse, status_code=status.HTTP_201_CREATED)
def create_device(payload: ServiceCreate, request: Request, db: Session = Depends(get_db)):
    """Add a new monitored device."""
    existing = db.query(MonitoredService).filter(MonitoredService.ip_address == payload.ip_address).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Device with IP address '{payload.ip_address}' already exists."
        )

    device = MonitoredService(
        name=payload.name,
        ip_address=payload.ip_address,
        area=payload.area,
        device_type=payload.device_type,
        is_maintenance=payload.is_maintenance,
        status="UP",
        response_time_ms=0.0
    )
    db.add(device)
    
    # Audit log
    audit = AuditLog(
        user_name="SYSTEM",
        action=f"Created device: {payload.name} ({payload.ip_address})",
        ip_address=request.client.host if request.client else "127.0.0.1",
        timestamp=datetime.utcnow()
    )
    db.add(audit)

    db.commit()
    db.refresh(device)
    return device


@router.put("/{device_id}", response_model=ServiceResponse)
def update_device(device_id: int, payload: ServiceUpdate, request: Request, db: Session = Depends(get_db)):
    """Update an existing monitored device."""
    device = db.query(MonitoredService).filter(MonitoredService.id == device_id).first()
    if not device:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Device not found.")

    if payload.ip_address and payload.ip_address != device.ip_address:
        existing = db.query(MonitoredService).filter(MonitoredService.ip_address == payload.ip_address).first()
        if existing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"IP address '{payload.ip_address}' is already assigned to another device."
            )

    update_data = payload.model_dump(exclude_unset=True) if hasattr(payload, "model_dump") else payload.dict(exclude_unset=True)
    for field, value in update_data.items():
        setattr(device, field, value)

    audit = AuditLog(
        user_name="SYSTEM",
        action=f"Updated device ID {device_id} ({device.name})",
        ip_address=request.client.host if request.client else "127.0.0.1",
        timestamp=datetime.utcnow()
    )
    db.add(audit)

    db.commit()
    db.refresh(device)
    return device


@router.delete("/{device_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_device(device_id: int, request: Request, db: Session = Depends(get_db)):
    """Delete a monitored device."""
    device = db.query(MonitoredService).filter(MonitoredService.id == device_id).first()
    if not device:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Device not found.")

    device_name = device.name
    device_ip = device.ip_address

    db.delete(device)

    audit = AuditLog(
        user_name="SYSTEM",
        action=f"Deleted device ID {device_id}: {device_name} ({device_ip})",
        ip_address=request.client.host if request.client else "127.0.0.1",
        timestamp=datetime.utcnow()
    )
    db.add(audit)

    db.commit()
    return None


@router.post("/{device_id}/toggle-maintenance", response_model=ServiceResponse)
def toggle_maintenance(device_id: int, request: Request, db: Session = Depends(get_db)):
    """Toggle the is_maintenance mode of a device."""
    device = db.query(MonitoredService).filter(MonitoredService.id == device_id).first()
    if not device:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Device not found.")

    device.is_maintenance = not device.is_maintenance

    audit = AuditLog(
        user_name="SYSTEM",
        action=f"Toggled maintenance for device '{device.name}' to {device.is_maintenance}",
        ip_address=request.client.host if request.client else "127.0.0.1",
        timestamp=datetime.utcnow()
    )
    db.add(audit)

    db.commit()
    db.refresh(device)
    return device


audit_router = APIRouter(prefix="/api/audit-logs", tags=["Audit Logs"])


@audit_router.get("")
def get_audit_logs(limit: int = 50, db: Session = Depends(get_db)):
    """List recent audit logs, newest first."""
    logs = db.query(AuditLog).order_by(AuditLog.timestamp.desc()).limit(limit).all()
    return [
        {
            "id": log.id,
            "user_name": log.user_name,
            "action": log.action,
            "ip_address": log.ip_address,
            "timestamp": log.timestamp.isoformat() if log.timestamp else None,
        }
        for log in logs
    ]
