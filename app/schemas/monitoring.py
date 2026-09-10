from datetime import datetime
from typing import Optional
from pydantic import BaseModel, ConfigDict
from app.models.monitoring import AreaZona, DeviceType


# ==========================================
# SERVICE SCHEMAS
# ==========================================
class ServiceBase(BaseModel):
    name: str
    ip_address: str
    area: AreaZona
    device_type: DeviceType
    is_maintenance: bool = False


class ServiceCreate(ServiceBase):
    pass


class ServiceUpdate(BaseModel):
    name: Optional[str] = None
    ip_address: Optional[str] = None
    area: Optional[AreaZona] = None
    device_type: Optional[DeviceType] = None
    status: Optional[str] = None
    response_time_ms: Optional[float] = None
    is_maintenance: Optional[bool] = None


class ServiceResponse(ServiceBase):
    id: int
    status: str = "UP"
    response_time_ms: float = 0.0
    last_check: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


# ==========================================
# INCIDENT SCHEMAS
# ==========================================
class IncidentBase(BaseModel):
    service_id: int
    severity: str  # "WARNING", "HIGH", "DISASTER"
    status: str = "NEW"  # "NEW", "ACKNOWLEDGED", "RESOLVED"
    ack_by: Optional[str] = None
    ack_message: Optional[str] = None


class IncidentCreate(BaseModel):
    service_id: int
    severity: str
    status: str = "NEW"


class IncidentAcknowledge(BaseModel):
    ack_by: str
    ack_message: Optional[str] = None


class IncidentResponse(IncidentBase):
    id: int
    created_at: datetime
    ack_at: Optional[datetime] = None
    resolved_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


# ==========================================
# AUDIT LOG SCHEMAS
# ==========================================
class AuditLogBase(BaseModel):
    user_name: str
    action: str
    ip_address: str


class AuditLogCreate(AuditLogBase):
    pass


class AuditLogResponse(AuditLogBase):
    id: int
    timestamp: datetime

    model_config = ConfigDict(from_attributes=True)
