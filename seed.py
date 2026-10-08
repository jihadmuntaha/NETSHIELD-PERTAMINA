import logging
from app.database import engine, SessionLocal, Base
from app.models import Device, AreaZona, DeviceType, SiteType

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("seed")


def seed_data():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()

    try:
        existing_count = db.query(Device).count()
        if existing_count > 0:
            logger.info("Database already contains %d devices. Skipping seed.", existing_count)
            return

        initial_devices = [
            {
                "name": "Prometheus Server / Core NMS",
                "ip_address": "127.0.0.1:9090",
                "location_name": "Regional HQ Semarang",
                "site_type": SiteType.REGIONAL_OFFICE.value,
                "level": 0,
                "area": AreaZona.ZONA_4,
                "device_type": DeviceType.ROUTER,
            },
            {
                "name": "Router Gate VPN",
                "ip_address": "10.4.12.1",
                "location_name": "FT Pengapon Semarang",
                "site_type": SiteType.FUEL_TERMINAL.value,
                "level": 1,
                "area": AreaZona.ZONA_1,
                "device_type": DeviceType.ROUTER,
            },
            {
                "name": "CCTV Gate Masuk Truk",
                "ip_address": "10.4.12.50",
                "location_name": "FT Pengapon Semarang",
                "site_type": SiteType.FUEL_TERMINAL.value,
                "level": 2,
                "area": AreaZona.ZONA_1,
                "device_type": DeviceType.CCTV,
            },
            {
                "name": "CCTV Thermal Tangki T-01",
                "ip_address": "10.4.13.20",
                "location_name": "FT Pengapon Semarang",
                "site_type": SiteType.FUEL_TERMINAL.value,
                "level": 2,
                "area": AreaZona.ZONA_2,
                "device_type": DeviceType.CCTV,
            },
            {
                "name": "Switch Outpost Tangki",
                "ip_address": "10.4.13.2",
                "location_name": "FT Pengapon Semarang",
                "site_type": SiteType.FUEL_TERMINAL.value,
                "level": 2,
                "area": AreaZona.ZONA_2,
                "device_type": DeviceType.SWITCH,
            },
            {
                "name": "Switch Presisi Island 01",
                "ip_address": "10.4.14.5",
                "location_name": "FT Pengapon Semarang",
                "site_type": SiteType.FUEL_TERMINAL.value,
                "level": 2,
                "area": AreaZona.ZONA_3,
                "device_type": DeviceType.SWITCH,
            },
            {
                "name": "CCTV Island Filling Shed",
                "ip_address": "10.4.14.60",
                "location_name": "FT Pengapon Semarang",
                "site_type": SiteType.FUEL_TERMINAL.value,
                "level": 2,
                "area": AreaZona.ZONA_3,
                "device_type": DeviceType.CCTV,
            },
            {
                "name": "Core Router Utama",
                "ip_address": "10.4.11.1",
                "location_name": "FT Pengapon Semarang",
                "site_type": SiteType.FUEL_TERMINAL.value,
                "level": 1,
                "area": AreaZona.ZONA_4,
                "device_type": DeviceType.ROUTER,
            },
            {
                "name": "Core Switch Backbone Depot",
                "ip_address": "10.4.11.2",
                "location_name": "FT Pengapon Semarang",
                "site_type": SiteType.FUEL_TERMINAL.value,
                "level": 2,
                "area": AreaZona.ZONA_4,
                "device_type": DeviceType.SWITCH,
            },
        ]

        for item in initial_devices:
            service = Device(
                name=item["name"],
                ip_address=item["ip_address"],
                location_name=item["location_name"],
                site_type=item["site_type"],
                level=item["level"],
                area=item["area"],
                device_type=item["device_type"],
                status="NORMAL",
                response_time_ms=0.0,
                is_maintenance=False
            )
            db.add(service)

        db.commit()
        logger.info("Successfully seeded FT Pengapon & Prometheus server target nodes.")

    except Exception as e:
        db.rollback()
        logger.error("Failed to seed database: %s", e)
        raise
    finally:
        db.close()


if __name__ == "__main__":
    seed_data()
