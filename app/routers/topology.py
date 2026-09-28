from typing import Dict, Any, List
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.models.device import Device
from app.models.monitoring import IncidentLog, MonitoredService
from seed_topology import seed_topology

router = APIRouter(prefix="/api/v1/topology", tags=["Topology"])
alias_router = APIRouter(prefix="/api/topology", tags=["Topology Alias"])


@router.get("/data")
@alias_router.get("/data")
def get_topology_data(db: Session = Depends(get_db)) -> Dict[str, Any]:
    """
    Endpoint API Visualisasi Topologi Jaringan Dinamis & Otomatis.
    Mengecek relasi parent_id (uplink) dari tabel devices serta insiden aktif di incident_logs.
    Kembalikan data JSON (nodes & edges) berstruktur hierarkis untuk Vis.js Network.
    """
    # 1. Pastikan tabel devices terisi (jalankan seeder jika belum ada data)
    devices = db.query(Device).all()
    if not devices:
        try:
            seed_topology()
            devices = db.query(Device).all()
        except Exception:
            pass

    # 2. Ambil insiden aktif (status NEW atau ACKNOWLEDGED) untuk mapping status kesehatan real-time
    active_incidents = (
        db.query(IncidentLog)
        .options(joinedload(IncidentLog.service))
        .filter(IncidentLog.status.in_(["NEW", "ACKNOWLEDGED"]))
        .all()
    )

    # Petakan status kesehatan berdasarkan ip_address dan nama service
    status_map: Dict[str, str] = {}
    for inc in active_incidents:
        if inc.service:
            ip = inc.service.ip_address
            clean_ip = ip.split(":")[0] if ip else ""
            severity = (inc.severity or "").upper()
            st = "CRITICAL" if severity in ["CRITICAL", "DISASTER", "HIGH"] else "WARNING"

            status_map[ip] = st
            if clean_ip:
                status_map[clean_ip] = st
            if inc.service.name:
                status_map[inc.service.name] = st

    # Cek juga status DOWN dari MonitoredService
    monitored_services = db.query(MonitoredService).all()
    for srv in monitored_services:
        if srv.status == "DOWN":
            ip = srv.ip_address
            clean_ip = ip.split(":")[0] if ip else ""
            status_map[ip] = "CRITICAL"
            if clean_ip:
                status_map[clean_ip] = "CRITICAL"
            if srv.name:
                status_map[srv.name] = "CRITICAL"

    # 3. Mapping Level Hierarki berdasarkan device_type
    level_map = {
        "gateway": 0,
        "core": 1,
        "distribution": 2,
        "access": 3
    }

    nodes = []
    edges = []

    for dev in devices:
        dev_type = (dev.device_type or "access").lower()
        level = level_map.get(dev_type, 3)

        clean_ip = dev.ip_address.split(":")[0] if dev.ip_address else ""

        # Tentukan status: CRITICAL | WARNING | UP
        dev_status = (
            status_map.get(dev.ip_address)
            or status_map.get(clean_ip)
            or status_map.get(dev.name)
            or "UP"
        )

        nodes.append({
            "id": dev.id,
            "label": f"{dev.name}\n({dev.ip_address})",
            "level": level,
            "status": dev_status,
            "ip": dev.ip_address,
            "zone": dev.zone or "Fuel Terminal",
            "device_type": dev.device_type
        })

        # Edge hanya dibuat jika parent_id tidak null (relasi uplink -> downlink)
        if dev.parent_id is not None:
            edges.append({
                "from": dev.parent_id,
                "to": dev.id,
                "arrows": "to"
            })

    return {
        "nodes": nodes,
        "edges": edges
    }
