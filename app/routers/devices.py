import io
import os
import re
import csv
import json
import ipaddress
import zipfile
import xml.etree.ElementTree as ET
from typing import List, Optional, Dict, Any
from datetime import datetime
import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, status, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.monitoring import MonitoredService, Device, AuditLog, AreaZona, DeviceType, SiteType
from app.schemas.monitoring import ServiceCreate, ServiceUpdate, ServiceResponse

router = APIRouter(prefix="/api/devices", tags=["Devices"])

# Router web (form HTML) untuk halaman Inventory: /devices, /devices/add, /devices/upload-csv, dst.
web_router = APIRouter(prefix="/devices", tags=["Devices Web"])

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEMPLATES_DIR = os.path.join(BASE_DIR, "app", "templates")
if not os.path.exists(TEMPLATES_DIR):
    TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")

templates = Jinja2Templates(directory=TEMPLATES_DIR)


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


# =============================================================================
# WEB INVENTORY (FORM) — /devices
# Halaman Inventory + Tambah / Edit / Hapus + Upload Bulk CSV.
# =============================================================================

MAX_CSV_BYTES = 2 * 1024 * 1024  # batas upload CSV: 2 MB
LEVEL_MIN, LEVEL_MAX = 0, 3      # 0: WAN, 1: Core, 2: Distribution, 3: Access
DEFAULT_LOCATION = "Regional HQ Semarang"
DEFAULT_SITE_TYPE = SiteType.FUEL_TERMINAL.value
SITE_TYPE_VALUES = [s.value for s in SiteType]
CSV_REQUIRED_HEADERS = ("name", "ip_address")
CSV_TEMPLATE_HEADERS = ("name", "ip_address", "device_type", "location_name", "site_type", "level", "parent_ip")

_ENDPOINT_RE = re.compile(r"^(\d{1,3}(?:\.\d{1,3}){3})(?::(\d{1,5}))?$")
_NULL_TOKENS = {"", "none", "null", "nan", "nil", "-"}


def parse_parent_id(raw: Any) -> Optional[int]:
    """
    Konversi input parent_id dari form ke int atau None (NULL di database).
    String kosong, "None", "null", non-numerik, atau <= 0 dianggap tidak valid -> None,
    sehingga tidak memicu IntegrityError / Foreign Key constraint error.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if text.lower() in _NULL_TOKENS:
        return None
    try:
        value = int(text)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def normalize_endpoint(raw: Any) -> Optional[str]:
    """Validasi IPv4 (opsional :port, mis. 127.0.0.1:9090). Mengembalikan string ternormalisasi atau None."""
    match = _ENDPOINT_RE.match(str(raw or "").strip())
    if not match:
        return None
    host, port = match.group(1), match.group(2)
    try:
        ipaddress.IPv4Address(host)
    except ValueError:
        return None
    if port is not None and not 1 <= int(port) <= 65535:
        return None
    return f"{host}:{int(port)}" if port is not None else host


def parse_level(raw: Any, default: int = 0) -> int:
    text = str(raw if raw is not None else "").strip()
    if not text:
        return default
    try:
        value = int(text)
    except ValueError:
        raise ValueError(f"level '{text}' harus berupa angka {LEVEL_MIN}-{LEVEL_MAX}")
    if not LEVEL_MIN <= value <= LEVEL_MAX:
        raise ValueError(f"level {value} di luar rentang {LEVEL_MIN}-{LEVEL_MAX}")
    return value


def parse_site_type(raw: Any) -> str:
    text = str(raw or "").strip().upper().replace(" ", "_").replace("-", "_")
    if not text:
        return DEFAULT_SITE_TYPE
    if text not in SITE_TYPE_VALUES:
        raise ValueError(f"site_type '{raw}' tidak dikenal (pilih: {', '.join(SITE_TYPE_VALUES)})")
    return text


def parse_device_type_strict(raw: Any) -> DeviceType:
    text = str(raw or "").strip().upper()
    if not text:
        return DeviceType.ROUTER
    try:
        return DeviceType(text)
    except ValueError:
        raise ValueError(f"device_type '{raw}' tidak dikenal (pilih: {', '.join(d.value for d in DeviceType)})")


def creates_cycle(device_id: int, new_parent_id: int, parent_map: Dict[int, Optional[int]]) -> bool:
    """True jika menjadikan new_parent_id sebagai parent device_id membentuk loop topologi."""
    seen = set()
    current: Optional[int] = new_parent_id
    while current is not None and current not in seen:
        if current == device_id:
            return True
        seen.add(current)
        current = parent_map.get(current)
    return False


def _current_user(request: Request) -> str:
    session = request.session if hasattr(request, "session") else {}
    return (session.get("user") if session else None) or "Operator On-Duty"


def _flash(request: Request, level: str, message: str, details: Optional[List[str]] = None) -> None:
    """Simpan pesan satu-kali di session (dibaca & dihapus oleh halaman /devices)."""
    details = [d[:160] for d in (details or [])]
    if len(details) > 8:
        extra = len(details) - 8
        details = details[:8] + [f"... dan {extra} pesan lainnya"]
    request.session["flash"] = {"level": level, "message": message, "details": details}


def _audit(db: Session, request: Request, action: str) -> None:
    db.add(AuditLog(
        user_name=_current_user(request),
        action=action[:100],
        ip_address=request.client.host if request.client else "127.0.0.1",
        timestamp=datetime.utcnow(),
    ))


def _back_to_inventory() -> RedirectResponse:
    return RedirectResponse(url="/devices", status_code=status.HTTP_303_SEE_OTHER)


def _build_device_fields(
    db: Session,
    *,
    name: Optional[str],
    ip_address: Optional[str],
    device_type: Optional[str],
    area: Optional[str],
    location_name: Optional[str],
    site_type: Optional[str],
    level: Optional[str],
    parent_id: Optional[str],
    device_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Validasi & normalisasi input form Tambah/Edit. Raise ValueError berisi pesan untuk operator."""
    name = (name or "").strip()
    if not name:
        raise ValueError("Nama perangkat wajib diisi.")
    if len(name) > 100:
        raise ValueError("Nama perangkat maksimal 100 karakter.")

    ip = normalize_endpoint(ip_address)
    if not ip:
        raise ValueError(f"IP Address '{(ip_address or '').strip()}' tidak valid. Gunakan format IPv4, mis. 10.10.0.1")
    duplicate = db.query(Device).filter(Device.ip_address == ip)
    if device_id is not None:
        duplicate = duplicate.filter(Device.id != device_id)
    if duplicate.first():
        raise ValueError(f"IP Address {ip} sudah dipakai perangkat lain.")

    try:
        dtype = parse_device_type_strict(device_type)
        stype = parse_site_type(site_type)
        level_val = parse_level(level)
    except ValueError as exc:
        raise ValueError(str(exc)[0].upper() + str(exc)[1:] + ".")

    # parent_id: "", "None", atau tidak valid -> NULL. Parent harus ada; tidak boleh diri sendiri / loop.
    parent = parse_parent_id(parent_id)
    if parent is not None:
        if device_id is not None and parent == device_id:
            raise ValueError("Perangkat tidak boleh memilih dirinya sendiri sebagai parent.")
        if db.query(Device.id).filter(Device.id == parent).first() is None:
            parent = None
        elif device_id is not None:
            parent_map = dict(db.query(Device.id, Device.parent_id).all())
            if creates_cycle(device_id, parent, parent_map):
                raise ValueError("Parent yang dipilih adalah turunan perangkat ini (membentuk loop topologi).")

    return {
        "name": name,
        "ip_address": ip,
        "device_type": dtype,
        "area": parse_area_value(area, name),
        "location_name": (location_name or "").strip()[:100] or DEFAULT_LOCATION,
        "site_type": stype,
        "level": level_val,
        "parent_id": parent,
    }


@web_router.get("", response_class=HTMLResponse)
@web_router.get("/", response_class=HTMLResponse)
def devices_inventory_view(request: Request, db: Session = Depends(get_db)):
    """GET /devices: halaman Inventory perangkat (tabel + modal Tambah / Edit / Upload CSV)."""
    devices = db.query(Device).order_by(Device.id.asc()).all()
    flash = request.session.pop("flash", None)

    return templates.TemplateResponse(
        request=request,
        name="devices.html",
        context={
            "request": request,
            "devices": devices,
            "name_by_id": {d.id: d.name for d in devices},
            "areas": [{"key": a.name, "label": a.value} for a in AreaZona],
            "site_types": SITE_TYPE_VALUES,
            "device_types": [d.value for d in DeviceType],
            "csv_headers": ",".join(CSV_TEMPLATE_HEADERS),
            "flash": flash,
            "active_page": "devices",
            "user": _current_user(request),
            "app_name": os.getenv("APP_NAME", "Pertamina NetShield"),
        },
    )


@web_router.post("/add")
def add_device_form(
    request: Request,
    name: Optional[str] = Form(None),
    ip_address: Optional[str] = Form(None),
    device_type: Optional[str] = Form(None),
    area: Optional[str] = Form(None),
    location_name: Optional[str] = Form(None),
    site_type: Optional[str] = Form(None),
    level: Optional[str] = Form(None),
    parent_id: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    """POST /devices/add: simpan perangkat baru (termasuk location_name, site_type, level, parent_id)."""
    try:
        fields = _build_device_fields(
            db, name=name, ip_address=ip_address, device_type=device_type, area=area,
            location_name=location_name, site_type=site_type, level=level, parent_id=parent_id,
        )
        device = Device(**fields, status="UP", response_time_ms=0.0, is_maintenance=False)
        db.add(device)
        _audit(db, request, f"Created device: {fields['name']} ({fields['ip_address']})")
        db.commit()
    except ValueError as exc:
        db.rollback()
        _flash(request, "danger", str(exc))
        return _back_to_inventory()
    except SQLAlchemyError as exc:
        db.rollback()
        _flash(request, "danger", f"Gagal menyimpan perangkat ke database: {exc.__class__.__name__}")
        return _back_to_inventory()

    _flash(request, "success", f"Perangkat '{fields['name']}' ({fields['ip_address']}) berhasil ditambahkan.")
    return _back_to_inventory()


@web_router.post("/upload-csv")
async def upload_devices_csv(request: Request, file: UploadFile = File(...), db: Session = Depends(get_db)):
    """
    POST /devices/upload-csv: impor massal perangkat dari CSV.

    Header: name,ip_address,device_type,location_name,site_type,level,parent_ip
    - IP yang sudah ada di database (atau duplikat di dalam file) dilewati.
    - parent_ip dipetakan ke parent_id setelah seluruh baris dibuat, sehingga parent boleh
      muncul di baris mana pun (atau sudah ada di database sebelumnya).
    """
    filename = file.filename or ""
    if not filename.lower().endswith(".csv"):
        _flash(request, "danger", "File harus berekstensi .csv")
        return _back_to_inventory()

    raw = await file.read()
    if not raw:
        _flash(request, "danger", "File CSV kosong.")
        return _back_to_inventory()
    if len(raw) > MAX_CSV_BYTES:
        _flash(request, "danger", f"Ukuran file melebihi batas {MAX_CSV_BYTES // (1024 * 1024)} MB.")
        return _back_to_inventory()

    try:
        text = raw.decode("utf-8-sig")  # utf-8-sig membuang BOM bawaan Excel
    except UnicodeDecodeError:
        text = raw.decode("latin-1")

    lines = text.splitlines()
    first_line = lines[0] if lines else ""
    delimiter = ";" if first_line.count(";") > first_line.count(",") else ","  # Excel locale Indonesia

    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    headers = [(h or "").strip().lower() for h in (reader.fieldnames or [])]
    missing = [h for h in CSV_REQUIRED_HEADERS if h not in headers]
    if missing:
        _flash(
            request, "danger",
            f"Header CSV tidak lengkap, kolom wajib hilang: {', '.join(missing)}.",
            [f"Header yang diharapkan: {','.join(CSV_TEMPLATE_HEADERS)}"],
        )
        return _back_to_inventory()

    def clean(value: Any) -> str:
        return value.strip() if isinstance(value, str) else ""

    ip_to_id: Dict[str, Optional[int]] = {ip: dev_id for ip, dev_id in db.query(Device.ip_address, Device.id).all()}
    created: List[tuple] = []  # (Device, parent_ip, nomor_baris)
    skipped_existing = 0
    errors: List[str] = []
    warnings: List[str] = []

    # --- Pass 1: buat seluruh perangkat (parent_id dikosongkan dulu) ---
    for line_no, row in enumerate(reader, start=2):
        row = {(k or "").strip().lower(): clean(v) for k, v in row.items() if k is not None}
        if not any(row.values()):
            continue  # baris kosong

        try:
            name = row.get("name", "")
            if not name:
                raise ValueError("kolom name kosong")
            if len(name) > 100:
                raise ValueError("name lebih dari 100 karakter")
            ip = normalize_endpoint(row.get("ip_address"))
            if not ip:
                raise ValueError(f"ip_address '{row.get('ip_address', '')}' tidak valid")
            if ip in ip_to_id:
                skipped_existing += 1
                continue
            dtype = parse_device_type_strict(row.get("device_type"))
            stype = parse_site_type(row.get("site_type"))
            level_val = parse_level(row.get("level"))
        except ValueError as exc:
            errors.append(f"Baris {line_no}: {exc}")
            continue

        location = row.get("location_name", "")[:100] or DEFAULT_LOCATION
        device = Device(
            name=name,
            ip_address=ip,
            device_type=dtype,
            area=infer_area_zona(name, location),
            location_name=location,
            site_type=stype,
            level=level_val,
            parent_id=None,
            status="UP",
            response_time_ms=0.0,
            is_maintenance=False,
        )
        db.add(device)
        created.append((device, row.get("parent_ip", ""), line_no))
        ip_to_id[ip] = None  # cadangkan IP agar duplikat di dalam file ikut terlewati

    try:
        db.flush()  # beri ID pada perangkat baru agar parent_ip bisa dipetakan

        for device, _, _ in created:
            ip_to_id[device.ip_address] = device.id

        # --- Pass 2: petakan parent_ip -> parent_id ---
        parent_map = dict(db.query(Device.id, Device.parent_id).all())
        for device, parent_ip, line_no in created:
            if parent_ip.strip().lower() in _NULL_TOKENS:
                continue
            parent_norm = normalize_endpoint(parent_ip)
            parent_id = ip_to_id.get(parent_norm) if parent_norm else None
            if parent_id is None:
                warnings.append(f"Baris {line_no}: parent_ip '{parent_ip}' tidak ditemukan, parent dikosongkan.")
            elif parent_id == device.id:
                warnings.append(f"Baris {line_no}: parent_ip sama dengan IP perangkat itu sendiri, diabaikan.")
            elif creates_cycle(device.id, parent_id, parent_map):
                warnings.append(f"Baris {line_no}: parent_ip '{parent_ip}' membentuk loop topologi, diabaikan.")
            else:
                device.parent_id = parent_id
                parent_map[device.id] = parent_id

        if created:
            _audit(db, request, f"Bulk CSV import '{filename}': added {len(created)} devices")
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        _flash(request, "danger", f"Import CSV dibatalkan karena kesalahan database: {exc.__class__.__name__}")
        return _back_to_inventory()

    summary = (
        f"Import CSV selesai: {len(created)} perangkat ditambahkan, "
        f"{skipped_existing} dilewati (IP sudah ada), {len(errors)} baris ditolak."
    )
    level = "success" if created and not errors else ("warning" if created or skipped_existing else "danger")
    _flash(request, level, summary, errors + warnings)
    return _back_to_inventory()


@web_router.get("/csv-template")
def download_csv_template():
    """GET /devices/csv-template: contoh file CSV untuk Bulk Upload."""
    sample = (
        ",".join(CSV_TEMPLATE_HEADERS) + "\n"
        "Core-Router-WAN,10.10.0.1,ROUTER,Regional HQ Semarang,REGIONAL_OFFICE,0,\n"
        "Dist-Switch-L2,10.10.2.1,SWITCH,Regional HQ Semarang,REGIONAL_OFFICE,2,10.10.0.1\n"
        "CCTV-Gate-Masuk,10.4.12.50,CCTV,FT Pengapon,FUEL_TERMINAL,3,10.10.2.1\n"
    )
    return Response(
        content="\ufeff" + sample,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="NetShield_Devices_Template.csv"'},
    )


@web_router.post("/{device_id}/edit")
def edit_device_form(
    device_id: int,
    request: Request,
    name: Optional[str] = Form(None),
    ip_address: Optional[str] = Form(None),
    device_type: Optional[str] = Form(None),
    area: Optional[str] = Form(None),
    location_name: Optional[str] = Form(None),
    site_type: Optional[str] = Form(None),
    level: Optional[str] = Form(None),
    parent_id: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    """POST /devices/{device_id}/edit: ubah perangkat (parent tidak boleh diri sendiri / turunannya)."""
    device = db.query(Device).filter(Device.id == device_id).first()
    if not device:
        _flash(request, "danger", f"Perangkat dengan ID #{device_id} tidak ditemukan.")
        return _back_to_inventory()

    try:
        fields = _build_device_fields(
            db, name=name, ip_address=ip_address, device_type=device_type, area=area,
            location_name=location_name, site_type=site_type, level=level, parent_id=parent_id,
            device_id=device_id,
        )
        for key, value in fields.items():
            setattr(device, key, value)
        _audit(db, request, f"Updated device ID {device_id} ({fields['name']})")
        db.commit()
    except ValueError as exc:
        db.rollback()
        _flash(request, "danger", str(exc))
        return _back_to_inventory()
    except SQLAlchemyError as exc:
        db.rollback()
        _flash(request, "danger", f"Gagal menyimpan perubahan ke database: {exc.__class__.__name__}")
        return _back_to_inventory()

    _flash(request, "success", f"Perangkat '{fields['name']}' berhasil diperbarui.")
    return _back_to_inventory()


@web_router.post("/{device_id}/delete")
def delete_device_form(device_id: int, request: Request, db: Session = Depends(get_db)):
    """
    POST /devices/{device_id}/delete: hapus perangkat.

    Perangkat anak (child) dinaikkan ke parent dari perangkat yang dihapus, bukan ikut terhapus
    (relasi children memakai cascade delete-orphan). Riwayat insiden perangkat ikut dihapus.
    """
    device = db.query(Device).filter(Device.id == device_id).first()
    if not device:
        _flash(request, "danger", f"Perangkat dengan ID #{device_id} tidak ditemukan.")
        return _back_to_inventory()

    name, ip, grandparent_id = device.name, device.ip_address, device.parent_id
    try:
        # Dieksekusi langsung (bukan lewat relationship) agar tidak memicu cascade ke anak-anaknya.
        reparented = (
            db.query(Device)
            .filter(Device.parent_id == device_id)
            .update({Device.parent_id: grandparent_id}, synchronize_session=False)
        )
        db.delete(device)
        _audit(db, request, f"Deleted device ID {device_id}: {name} ({ip})")
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        _flash(request, "danger", f"Gagal menghapus perangkat: {exc.__class__.__name__}")
        return _back_to_inventory()

    suffix = f" {reparented} perangkat turunan dipindahkan ke parent di atasnya." if reparented else ""
    _flash(request, "success", f"Perangkat '{name}' ({ip}) berhasil dihapus.{suffix}")
    return _back_to_inventory()


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
