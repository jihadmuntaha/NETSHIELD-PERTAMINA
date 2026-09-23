from typing import Optional
from datetime import datetime
import enum
from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, Text, ForeignKey, Enum as SQLEnum
from sqlalchemy.orm import relationship
from app.database import Base


class AreaZona(str, enum.Enum):
    ZONA_1 = "Zona 1 - Gate & Main Office"
    ZONA_2 = "Zona 2 - Tank Farm (Tangki Timbun)"
    ZONA_3 = "Zona 3 - Filling Shed (Loading Bay)"
    ZONA_4 = "Zona 4 - IT Server Room & Core Network"


class DeviceType(str, enum.Enum):
    ROUTER = "ROUTER"
    SWITCH = "SWITCH"
    CCTV = "CCTV"


class MonitoredService(Base):
    __tablename__ = "monitored_services"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)
    ip_address = Column(String(45), unique=True, index=True, nullable=False)
    area = Column(SQLEnum(AreaZona), nullable=False)
    device_type = Column(SQLEnum(DeviceType), nullable=False)
    status = Column(String(20), default="UP", nullable=False)
    response_time_ms = Column(Float, default=0.0, nullable=False)
    is_maintenance = Column(Boolean, default=False, nullable=False)
    last_check = Column(DateTime, nullable=True)

    incidents = relationship("IncidentLog", back_populates="service", cascade="all, delete-orphan")


class IncidentLog(Base):
    __tablename__ = "incident_logs"

    id = Column(Integer, primary_key=True, index=True)
    service_id = Column(Integer, ForeignKey("monitored_services.id"), nullable=False)
    severity = Column(String(20), nullable=False)  # "WARNING", "CRITICAL", "HIGH", "DISASTER"
    title = Column(String(255), nullable=True)
    latency_ms = Column(Float, nullable=True)
    status = Column(String(20), default="NEW", nullable=False)  # "NEW", "ACKNOWLEDGED", "RESOLVED"
    ack_by = Column(String(100), nullable=True)
    ack_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    ack_at = Column(DateTime, nullable=True)
    resolved_at = Column(DateTime, nullable=True)

    service = relationship("MonitoredService", back_populates="incidents")

    @property
    def is_acknowledged(self) -> bool:
        return self.status == "ACKNOWLEDGED"

    @is_acknowledged.setter
    def is_acknowledged(self, val: bool):
        if val:
            self.status = "ACKNOWLEDGED"
        elif self.status == "ACKNOWLEDGED":
            self.status = "NEW"

    @property
    def acknowledged_by(self) -> Optional[str]:
        return self.ack_by

    @acknowledged_by.setter
    def acknowledged_by(self, val: Optional[str]):
        self.ack_by = val

    @property
    def acknowledged_at(self) -> Optional[datetime]:
        return self.ack_at

    @acknowledged_at.setter
    def acknowledged_at(self, val: Optional[datetime]):
        self.ack_at = val


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, index=True)
    user_name = Column(String(100), nullable=False)
    action = Column(String(100), nullable=False)
    ip_address = Column(String(45), nullable=False)
    timestamp = Column(DateTime, default=datetime.utcnow, nullable=False)
