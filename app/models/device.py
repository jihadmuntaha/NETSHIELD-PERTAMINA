from sqlalchemy import Column, Integer, String, ForeignKey
from sqlalchemy.orm import relationship
from app.database import Base


class Device(Base):
    """
    Model SQLAlchemy Device untuk manajemen topologi hierarki dinamis berbasis parent_id (uplink).
    """
    __tablename__ = "devices"

    id = Column(Integer, primary_key=True, autoincrement=True, index=True)
    name = Column(String(100), unique=True, nullable=False, index=True)
    ip_address = Column(String(45), nullable=False, index=True)
    device_type = Column(String(50), default="access", nullable=False)  # "gateway", "core", "distribution", "access"
    zone = Column(String(50), nullable=True)
    parent_id = Column(Integer, ForeignKey("devices.id"), nullable=True)

    # Self-referential relationship untuk uplink dan downlinks
    uplink = relationship("Device", remote_side=[id], back_populates="downlinks")
    downlinks = relationship("Device", back_populates="uplink", cascade="all, delete-orphan")
