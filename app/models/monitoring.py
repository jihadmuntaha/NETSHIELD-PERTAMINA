"""
app/models/monitoring.py
========================
Model database SQLAlchemy untuk Pertamina NetShield NOC enterprise multi-lokasi (Pertamina Patra Niaga Region JBT).
"""

from typing import Optional
from datetime import datetime
import enum
from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, Text, ForeignKey, Enum as SQLEnum
from sqlalchemy.orm import relationship, backref
from app.database import Base


class AreaZona(str, enum.Enum):
    ZONA_1 = "Zona 1 - Gate & Main Office"
    ZONA_2 = "Zona 2 - Tank Farm (Tangki Timbun)"
    ZONA_3 = "Zona 3 - Filling Shed (Loading Bay)"
    ZONA_4 = "Zona 4 - IT Server Room & Core Network"


class SiteType(str, enum.Enum):
    REGIONAL_OFFICE = "REGIONAL_OFFICE"
    FUEL_TERMINAL = "FUEL_TERMINAL"
    INTEGRATED_TERMINAL = "INTEGRATED_TERMINAL"
    AFT = "AFT"


class DeviceType(str, enum.Enum):
    ROUTER = "ROUTER"
    SWITCH = "SWITCH"
    CCTV = "CCTV"


class Device(Base):
    __tablename__ = "devices"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)
    ip_address = Column(String(45), unique=True, index=True, nullable=False)
    location_name = Column(String(100), nullable=False, default="Regional HQ Semarang")
    site_type = Column(String(50), nullable=False, default="FUEL_TERMINAL")
    level = Column(Integer, default=0, nullable=False)  # 0: WAN, 1: Core, 2: Dist, 3: Access
    parent_id = Column(Integer, ForeignKey("devices.id"), nullable=True)
    area = Column(SQLEnum(AreaZona), nullable=True, default=AreaZona.ZONA_1)
    device_type = Column(SQLEnum(DeviceType), nullable=True, default=DeviceType.ROUTER)
    status = Column(String(20), default="NORMAL", nullable=False)  # NORMAL, WARNING, CRITICAL, DOWN, UP
    response_time_ms = Column(Float, default=0.0, nullable=False)
    is_maintenance = Column(Boolean, default=False, nullable=False)
    last_check = Column(DateTime, nullable=True)

    # Relasi Self-Referencing Parent-Child
    children = relationship("Device", backref=backref("parent", remote_side=[id]), cascade="all, delete-orphan")

    # Relasi Bidirectional dengan IncidentLog
    incidents = relationship("IncidentLog", back_populates="device", cascade="all, delete-orphan")


# Alias MonitoredService -> Device untuk backward compatibility
MonitoredService = Device


class IncidentLog(Base):
    __tablename__ = "incident_logs"

    id = Column(Integer, primary_key=True, index=True)
    device_id = Column(Integer, ForeignKey("devices.id"), nullable=False)
    severity = Column(String(20), nullable=False)  # "WARNING", "CRITICAL"
    title = Column(String(255), nullable=True)
    latency_ms = Column(Float, nullable=True)
    status = Column(String(20), default="NEW", nullable=False)  # "NEW", "IN_PROGRESS", "ACKNOWLEDGED", "RESOLVED"
    
    assigned_to = Column(String(100), nullable=True)
    resolution_notes = Column(Text, nullable=True)

    ack_by = Column(String(100), nullable=True)
    ack_message = Column(Text, nullable=True)

    # Kolom SLA (Service Level Agreement)
    started_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    acknowledged_at = Column(DateTime, nullable=True)
    resolved_at = Column(DateTime, nullable=True)
    tta_seconds = Column(Float, nullable=True)
    ttr_seconds = Column(Float, nullable=True)

    # Relasi Bidirectional dengan Device
    device = relationship("Device", back_populates="incidents")

    # Backward compatibility properties
    @property
    def service_id(self) -> int:
        return self.device_id

    @service_id.setter
    def service_id(self, val: int):
        self.device_id = val

    @property
    def service(self) -> Optional[Device]:
        return self.device

    @property
    def created_at(self) -> datetime:
        return self.started_at

    @created_at.setter
    def created_at(self, val: datetime):
        self.started_at = val

    @property
    def ack_at(self) -> Optional[datetime]:
        return self.acknowledged_at

    @ack_at.setter
    def ack_at(self, val: Optional[datetime]):
        self.acknowledged_at = val

    @property
    def is_acknowledged(self) -> bool:
        return self.status in ("ACKNOWLEDGED", "IN_PROGRESS")

    @is_acknowledged.setter
    def is_acknowledged(self, val: bool):
        if val:
            if self.status not in ("RESOLVED", "IN_PROGRESS"):
                self.status = "ACKNOWLEDGED"
        else:
            if self.status == "ACKNOWLEDGED":
                self.status = "NEW"

    @property
    def acknowledged_by(self) -> Optional[str]:
        return self.assigned_to or self.ack_by

    @acknowledged_by.setter
    def acknowledged_by(self, val: Optional[str]):
        self.assigned_to = val
        self.ack_by = val


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, index=True)
    user_name = Column(String(100), nullable=False)
    action = Column(String(100), nullable=False)
    ip_address = Column(String(45), nullable=False)
    timestamp = Column(DateTime, default=datetime.utcnow, nullable=False)
