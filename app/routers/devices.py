import io
import re
import csv
import json
import zipfile
import xml.etree.ElementTree as ET
from typing import List, Optional, Dict, Any
from datetime import datetime
import pandas as pd
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


def parse_area_value(val: Optional[str], name_fallback: str = "") -> AreaZona:
    if not val:
        return infer_area_zona(name_fallback, "")
    val_str = str(val).strip().lower()
    for item in AreaZona:
        if item.value.lower() == val_str or item.name.lower() == val_str:
            return item
    return infer_area_zona(str(val), name_fallback)


def parse_device_type_value(val: Optional[str], name_fallback: str = "") -> DeviceType:
    if not val:
        return infer_device_type(name_fallback, "")
    val_str = str(val).strip().upper()
    for item in DeviceType:
        if item.value == val_str or item.name == val_str:
            return item
    return infer_device_type(str(val), name_fallback)


@router.post("/upload-assets", status_code=status.HTTP_201_CREATED)
@router.post("/upload-kmz", status_code=status.HTTP_201_CREATED)
async def upload_assets(request: Request, file: UploadFile = File(...), db: Session = Depends(get_db)):
    """Upload and parse network asset files (.kmz, .kml, .csv, .json, .geojson, .xlsx, .xls, .txt) to import monitored devices."""
    filename_lower = file.filename.lower()
    allowed_exts = ('.kmz', '.kml', '.csv', '.json', '.geojson', '.xlsx', '.xls', '.txt', '.zip')
    if not filename_lower.endswith(allowed_exts):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Format file tidak didukung. Harap unggah file ber-ekstensi .kmz, .kml, .csv, .json, .geojson, .xlsx, .xls, atau .txt"
        )

    file_bytes = await file.read()
    existing_services = db.query(MonitoredService).all()
    existing_ips = {s.ip_address for s in existing_services}
    ip_regex = re.compile(r'\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b')

    parsed_records = []

    # 1. KMZ / KML / ZIP Parsing
    if filename_lower.endswith(('.kmz', '.kml', '.zip')):
        kml_content = None
        if filename_lower.endswith('.kml'):
            kml_content = file_bytes
        else:
            try:
                with zipfile.ZipFile(io.BytesIO(file_bytes)) as z:
                    kml_filename = None
                    for name in z.namelist():
                        if name.lower().endswith('.kml'):
                            kml_filename = name
                            if name.lower() == 'doc.kml':
                                break
                    if not kml_filename:
                        raise HTTPException(
                            status_code=status.HTTP_400_BAD_REQUEST,
                            detail="File .kml tidak ditemukan di dalam arsip KMZ/ZIP."
                        )
                    kml_content = z.read(kml_filename)
            except zipfile.BadZipFile:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Arsip file .kmz/.zip rusak atau tidak valid."
                )

        try:
            root = ET.fromstring(kml_content)
        except ET.ParseError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Gagal memproses XML dari KML document: {str(e)}"
            )

        placemarks = [elem for elem in root.iter() if get_tag_name(elem).lower() == 'placemark']
        if not placemarks:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Tidak ada elemen <Placemark> yang ditemukan di file KMZ/KML."
            )

        for placemark in placemarks:
            name = find_first_text(placemark, 'name') or f"Placemark Node {len(parsed_records) + 1}"
            desc = find_first_text(placemark, 'description') or ""
            coords = find_first_text(placemark, 'coordinates') or ""

            found_ips = ip_regex.findall(name + " " + desc)
            device_ip = None
            for candidate_ip in found_ips:
                if candidate_ip not in existing_ips:
                    device_ip = candidate_ip
                    break

            parsed_records.append({
                "name": name,
                "ip_address": device_ip,
                "area_raw": desc,
                "type_raw": desc,
                "coords": coords
            })

    # 2. CSV Parsing
    elif filename_lower.endswith('.csv'):
        try:
            text = file_bytes.decode('utf-8', errors='ignore')
            reader = csv.DictReader(io.StringIO(text))
            rows = list(reader)
            if rows and reader.fieldnames:
                for row in rows:
                    norm = {str(k).strip().lower().replace(" ", "_"): str(v).strip() for k, v in row.items() if k}
                    name = norm.get("name") or norm.get("nama") or norm.get("device_name") or norm.get("hostname") or "CSV Device"
                    ip = norm.get("ip") or norm.get("ip_address") or norm.get("ip_addr") or norm.get("host") or ""
                    area = norm.get("area") or norm.get("zona") or norm.get("location") or ""
                    dev_type = norm.get("type") or norm.get("device_type") or norm.get("tipe") or ""
                    parsed_records.append({
                        "name": name,
                        "ip_address": ip if ip_regex.match(ip) else None,
                        "area_val": area,
                        "type_val": dev_type
                    })
            else:
                # Raw text lines fallback
                lines = text.splitlines()
                for line in lines:
                    line_str = line.strip()
                    if not line_str:
                        continue
                    found = ip_regex.findall(line_str)
                    ip_val = found[0] if found else None
                    name_val = ip_regex.sub('', line_str).strip(" ,;\t") or f"Device {len(parsed_records) + 1}"
                    parsed_records.append({"name": name_val, "ip_address": ip_val})
        except Exception as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Gagal membaca file CSV: {str(e)}")

    # 3. JSON / GeoJSON Parsing
    elif filename_lower.endswith(('.json', '.geojson')):
        try:
            text = file_bytes.decode('utf-8', errors='ignore')
            data = json.loads(text)

            items = []
            if isinstance(data, dict):
                if data.get("type") == "FeatureCollection" and isinstance(data.get("features"), list):
                    for feat in data["features"]:
                        props = feat.get("properties", {})
                        geom = feat.get("geometry", {})
                        coords = str(geom.get("coordinates", ""))
                        name = props.get("name") or props.get("nama") or props.get("title") or "GeoJSON Node"
                        ip = props.get("ip") or props.get("ip_address") or ""
                        items.append({"name": name, "ip_address": ip, "area_val": props.get("area"), "type_val": props.get("type"), "coords": coords})
                elif "devices" in data and isinstance(data["devices"], list):
                    items = data["devices"]
                elif "assets" in data and isinstance(data["assets"], list):
                    items = data["assets"]
                else:
                    items = [data]
            elif isinstance(data, list):
                items = data

            for item in items:
                if isinstance(item, dict):
                    name = item.get("name") or item.get("nama") or item.get("device_name") or "JSON Asset"
                    ip = item.get("ip") or item.get("ip_address") or item.get("host") or ""
                    parsed_records.append({
                        "name": str(name),
                        "ip_address": str(ip) if ip_regex.match(str(ip)) else None,
                        "area_val": item.get("area") or item.get("zona"),
                        "type_val": item.get("device_type") or item.get("type") or item.get("tipe"),
                        "coords": str(item.get("coords") or item.get("coordinates") or "")
                    })
        except Exception as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Gagal memproses file JSON/GeoJSON: {str(e)}")

    # 4. Excel Parsing (.xlsx, .xls)
    elif filename_lower.endswith(('.xlsx', '.xls')):
        try:
            df = pd.read_excel(io.BytesIO(file_bytes))
            df.columns = [str(c).strip().lower().replace(" ", "_") for c in df.columns]
            for _, row in df.iterrows():
                name = row.get("name") or row.get("nama") or row.get("device_name") or row.get("hostname") or "Excel Device"
                ip = row.get("ip") or row.get("ip_address") or row.get("ip_addr") or ""
                area = row.get("area") or row.get("zona") or ""
                dev_type = row.get("type") or row.get("device_type") or row.get("tipe") or ""
                ip_str = str(ip).strip() if pd.notna(ip) else ""
                parsed_records.append({
                    "name": str(name) if pd.notna(name) else "Excel Device",
                    "ip_address": ip_str if ip_regex.match(ip_str) else None,
                    "area_val": str(area) if pd.notna(area) else None,
                    "type_val": str(dev_type) if pd.notna(dev_type) else None
                })
        except Exception as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Gagal memproses file Excel: {str(e)}")

    # 5. TXT Parsing
    elif filename_lower.endswith('.txt'):
        try:
            text = file_bytes.decode('utf-8', errors='ignore')
            lines = text.splitlines()
            for line in lines:
                line_str = line.strip()
                if not line_str or line_str.startswith("#"):
                    continue
                found = ip_regex.findall(line_str)
                ip_val = found[0] if found else None
                name_val = ip_regex.sub('', line_str).strip(" ,;\t:-") or f"Asset Node {len(parsed_records) + 1}"
                parsed_records.append({"name": name_val, "ip_address": ip_val})
        except Exception as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Gagal memproses file TXT: {str(e)}")

    if not parsed_records:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Tidak ada data perangkat valid yang dapat diekstrak dari file."
        )

    imported_devices = []
    for rec in parsed_records:
        name = rec.get("name", "Asset Device")[:100]
        ip_cand = rec.get("ip_address")
        
        # Determine Area and Device Type
        if "area_val" in rec and rec["area_val"]:
            area = parse_area_value(str(rec["area_val"]), name)
        else:
            area = infer_area_zona(name, rec.get("area_raw", ""))

        if "type_val" in rec and rec["type_val"]:
            device_type = parse_device_type_value(str(rec["type_val"]), name)
        else:
            device_type = infer_device_type(name, rec.get("type_raw", ""))

        # IP Address allocation / verification
        if ip_cand and ip_cand not in existing_ips:
            device_ip = ip_cand
            existing_ips.add(device_ip)
        else:
            device_ip = generate_available_ip(area, existing_ips)

        service = MonitoredService(
            name=name,
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
            "coordinates": rec.get("coords", "")
        })

    client_ip = request.client.host if request.client else "127.0.0.1"
    audit = AuditLog(
        user_name="SYSTEM",
        action=f"Imported assets file '{file.filename}': added {len(imported_devices)} devices",
        ip_address=client_ip,
        timestamp=datetime.utcnow()
    )
    db.add(audit)

    db.commit()

    return {
        "status": "success",
        "message": f"Berhasil mengimpor {len(imported_devices)} perangkat dari file '{file.filename}'.",
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
