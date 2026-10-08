"""
seed_pertamina_jbt.py
=====================
Skrip inisialisasi database Pertamina NetShield NOC enterprise multi-lokasi (Pertamina Patra Niaga Region Jawa Bagian Tengah).
Menyediakan hierarki unit operasi Pertamina JBT lengkap dengan Prometheus Server NMS & perangkat FT Pengapon.
"""

import logging
from datetime import datetime, timedelta
from app.database import engine, SessionLocal, Base
from app.models import Device, IncidentLog, SiteType, AreaZona, DeviceType

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("seed_pertamina_jbt")


def seed_database():
    """
    Drop & create all tables, kemudian injeksi hierarki WAN/LAN unit operasi Pertamina JBT,
    Prometheus Server NMS (127.0.0.1:9090), perangkat FT Pengapon, dan sample tiket insiden awal.
    """
    logger.info("Dropping all existing tables in database...")
    Base.metadata.drop_all(bind=engine)
    logger.info("Creating all tables from Base metadata...")
    Base.metadata.create_all(bind=engine)

    db = SessionLocal()
    try:
        logger.info("Injecting Pertamina JBT multi-location topology hierarchy & Prometheus Server...")

        # ---------------------------------------------------------------------
        # 1. Regional HQ Semarang (Site Type: REGIONAL_OFFICE)
        # ---------------------------------------------------------------------
        hq_core = Device(
            name="HQ-Core-Router-WAN",
            ip_address="10.10.0.1",
            location_name="Regional HQ Semarang",
            site_type=SiteType.REGIONAL_OFFICE.value,
            level=0,
            parent_id=None,
            area=AreaZona.ZONA_4,
            device_type=DeviceType.ROUTER,
            status="NORMAL",
            response_time_ms=4.5
        )
        db.add(hq_core)
        db.flush()

        hq_fw = Device(
            name="HQ-Firewall-SecEdge",
            ip_address="10.10.1.1",
            location_name="Regional HQ Semarang",
            site_type=SiteType.REGIONAL_OFFICE.value,
            level=1,
            parent_id=hq_core.id,
            area=AreaZona.ZONA_4,
            device_type=DeviceType.ROUTER,
            status="NORMAL",
            response_time_ms=5.8
        )
        db.add(hq_fw)
        db.flush()

        hq_dist = Device(
            name="HQ-Dist-Switch-L2",
            ip_address="10.10.2.1",
            location_name="Regional HQ Semarang",
            site_type=SiteType.REGIONAL_OFFICE.value,
            level=2,
            parent_id=hq_fw.id,
            area=AreaZona.ZONA_1,
            device_type=DeviceType.SWITCH,
            status="NORMAL",
            response_time_ms=7.2
        )
        db.add(hq_dist)
        db.flush()

        # Prometheus Server Node
        prom_server = Device(
            name="Prometheus Server / Core NMS",
            ip_address="127.0.0.1:9090",
            location_name="Regional HQ Semarang",
            site_type=SiteType.REGIONAL_OFFICE.value,
            level=3,
            parent_id=hq_dist.id,
            area=AreaZona.ZONA_4,
            device_type=DeviceType.ROUTER,
            status="NORMAL",
            response_time_ms=1.8
        )
        db.add(prom_server)

        # ---------------------------------------------------------------------
        # 2. Integrated Terminal Semarang / FT Pengapon (Site Type: INTEGRATED_TERMINAL)
        # ---------------------------------------------------------------------
        it_smg_router = Device(
            name="IT-SMG-Edge-Router",
            ip_address="10.20.1.1",
            location_name="IT Semarang",
            site_type=SiteType.INTEGRATED_TERMINAL.value,
            level=1,
            parent_id=hq_core.id,
            area=AreaZona.ZONA_4,
            device_type=DeviceType.ROUTER,
            status="NORMAL",
            response_time_ms=11.4
        )
        db.add(it_smg_router)
        db.flush()

        it_smg_switch = Device(
            name="IT-SMG-Dist-Switch-Tangki",
            ip_address="10.20.2.1",
            location_name="IT Semarang",
            site_type=SiteType.INTEGRATED_TERMINAL.value,
            level=2,
            parent_id=it_smg_router.id,
            area=AreaZona.ZONA_2,
            device_type=DeviceType.SWITCH,
            status="NORMAL",
            response_time_ms=14.1
        )
        db.add(it_smg_switch)
        db.flush()

        it_smg_server = Device(
            name="IT-SMG-Server-TAS",
            ip_address="10.20.3.10",
            location_name="IT Semarang",
            site_type=SiteType.INTEGRATED_TERMINAL.value,
            level=3,
            parent_id=it_smg_switch.id,
            area=AreaZona.ZONA_4,
            device_type=DeviceType.SWITCH,
            status="NORMAL",
            response_time_ms=16.8
        )
        db.add(it_smg_server)

        # Legacy FT Pengapon Nodes
        pengapon_devices = [
            ("Router Gate VPN", "10.4.12.1", AreaZona.ZONA_1, DeviceType.ROUTER, 2, it_smg_router.id),
            ("CCTV Gate Masuk Truk", "10.4.12.50", AreaZona.ZONA_1, DeviceType.CCTV, 3, it_smg_switch.id),
            ("CCTV Thermal Tangki T-01", "10.4.13.20", AreaZona.ZONA_2, DeviceType.CCTV, 3, it_smg_switch.id),
            ("Switch Outpost Tangki", "10.4.13.2", AreaZona.ZONA_2, DeviceType.SWITCH, 2, it_smg_router.id),
            ("Switch Presisi Island 01", "10.4.14.5", AreaZona.ZONA_3, DeviceType.SWITCH, 2, it_smg_router.id),
            ("CCTV Island Filling Shed", "10.4.14.60", AreaZona.ZONA_3, DeviceType.CCTV, 3, it_smg_switch.id),
            ("Core Router Utama FT Pengapon", "10.4.11.1", AreaZona.ZONA_4, DeviceType.ROUTER, 1, hq_core.id),
            ("Core Switch Backbone Depot", "10.4.11.2", AreaZona.ZONA_4, DeviceType.SWITCH, 2, it_smg_router.id),
        ]

        for pname, pip, parea, ptype, plevel, pparent in pengapon_devices:
            dev = Device(
                name=pname,
                ip_address=pip,
                location_name="FT Pengapon Semarang",
                site_type=SiteType.FUEL_TERMINAL.value,
                level=plevel,
                parent_id=pparent,
                area=parea,
                device_type=ptype,
                status="NORMAL",
                response_time_ms=12.0
            )
            db.add(dev)

        # ---------------------------------------------------------------------
        # 3. Fuel Terminal Boyolali (Site Type: FUEL_TERMINAL)
        # ---------------------------------------------------------------------
        ft_byl_router = Device(
            name="FT-BYL-Edge-Router",
            ip_address="10.30.1.1",
            location_name="FT Boyolali",
            site_type=SiteType.FUEL_TERMINAL.value,
            level=1,
            parent_id=hq_core.id,
            area=AreaZona.ZONA_4,
            device_type=DeviceType.ROUTER,
            status="NORMAL",
            response_time_ms=21.3
        )
        db.add(ft_byl_router)
        db.flush()

        ft_byl_switch = Device(
            name="FT-BYL-Switch-FillingShed",
            ip_address="10.30.2.1",
            location_name="FT Boyolali",
            site_type=SiteType.FUEL_TERMINAL.value,
            level=2,
            parent_id=ft_byl_router.id,
            area=AreaZona.ZONA_3,
            device_type=DeviceType.SWITCH,
            status="NORMAL",
            response_time_ms=24.9
        )
        db.add(ft_byl_switch)
        db.flush()

        ft_byl_atg = Device(
            name="FT-BYL-ATG-Gateway",
            ip_address="10.30.3.5",
            location_name="FT Boyolali",
            site_type=SiteType.FUEL_TERMINAL.value,
            level=3,
            parent_id=ft_byl_switch.id,
            area=AreaZona.ZONA_2,
            device_type=DeviceType.SWITCH,
            status="WARNING",
            response_time_ms=284.0
        )
        db.add(ft_byl_atg)

        # ---------------------------------------------------------------------
        # 4. Fuel Terminal Rewulu (Site Type: FUEL_TERMINAL)
        # ---------------------------------------------------------------------
        ft_rwl_router = Device(
            name="FT-RWL-Edge-Router",
            ip_address="10.40.1.1",
            location_name="FT Rewulu",
            site_type=SiteType.FUEL_TERMINAL.value,
            level=1,
            parent_id=hq_core.id,
            area=AreaZona.ZONA_4,
            device_type=DeviceType.ROUTER,
            status="NORMAL",
            response_time_ms=28.7
        )
        db.add(ft_rwl_router)
        db.flush()

        ft_rwl_switch = Device(
            name="FT-RWL-Switch-ControlRoom",
            ip_address="10.40.2.1",
            location_name="FT Rewulu",
            site_type=SiteType.FUEL_TERMINAL.value,
            level=2,
            parent_id=ft_rwl_router.id,
            area=AreaZona.ZONA_1,
            device_type=DeviceType.SWITCH,
            status="CRITICAL",
            response_time_ms=0.0
        )
        db.add(ft_rwl_switch)

        # ---------------------------------------------------------------------
        # 5. Integrated Terminal Cilacap (Site Type: INTEGRATED_TERMINAL)
        # ---------------------------------------------------------------------
        it_clp_router = Device(
            name="IT-CLP-Edge-Router",
            ip_address="10.50.1.1",
            location_name="IT Cilacap",
            site_type=SiteType.INTEGRATED_TERMINAL.value,
            level=1,
            parent_id=hq_core.id,
            area=AreaZona.ZONA_4,
            device_type=DeviceType.ROUTER,
            status="NORMAL",
            response_time_ms=33.2
        )
        db.add(it_clp_router)

        db.commit()

        # ---------------------------------------------------------------------
        # Injeksi Sample Tiket Insiden Awal
        # ---------------------------------------------------------------------
        logger.info("Injecting sample incident tickets...")
        now = datetime.utcnow()

        inc_warning = IncidentLog(
            device_id=ft_byl_atg.id,
            severity="WARNING",
            title="High Latency RTT ~284ms",
            latency_ms=284.0,
            status="NEW",
            started_at=now - timedelta(minutes=15)
        )
        db.add(inc_warning)

        inc_critical = IncidentLog(
            device_id=ft_rwl_switch.id,
            severity="CRITICAL",
            title="Host Down (up=0)",
            latency_ms=0.0,
            status="NEW",
            started_at=now - timedelta(minutes=42)
        )
        db.add(inc_critical)

        db.commit()
        logger.info("Pertamina JBT database successfully seeded with devices including Prometheus Server NMS & FT Pengapon!")

    except Exception as e:
        db.rollback()
        logger.error("Failed to seed Pertamina JBT database: %s", e)
        raise
    finally:
        db.close()


if __name__ == "__main__":
    seed_database()
