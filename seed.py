import logging
from app.database import engine, SessionLocal, Base
from app.models.monitoring import MonitoredService, AreaZona, DeviceType

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("seed")


def seed_data():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()

    try:
        existing_count = db.query(MonitoredService).count()
        if existing_count > 0:
            logger.info("Database already contains %d services. Skipping seed.", existing_count)
            return

        initial_devices = [
            {
                "name": "Router Gate VPN",
                "ip_address": "10.4.12.1",
                "area": AreaZona.ZONA_1,
                "device_type": DeviceType.ROUTER,
            },
            {
                "name": "CCTV Gate Masuk Truk",
                "ip_address": "10.4.12.50",
                "area": AreaZona.ZONA_1,
                "device_type": DeviceType.CCTV,
            },
            {
                "name": "CCTV Thermal Tangki T-01",
                "ip_address": "10.4.13.20",
                "area": AreaZona.ZONA_2,
                "device_type": DeviceType.CCTV,
            },
            {
                "name": "Switch Outpost Tangki",
                "ip_address": "10.4.13.2",
                "area": AreaZona.ZONA_2,
                "device_type": DeviceType.SWITCH,
            },
            {
                "name": "Switch Presisi Island 01",
                "ip_address": "10.4.14.5",
                "area": AreaZona.ZONA_3,
                "device_type": DeviceType.SWITCH,
            },
            {
                "name": "CCTV Island Filling Shed",
                "ip_address": "10.4.14.60",
                "area": AreaZona.ZONA_3,
                "device_type": DeviceType.CCTV,
            },
            {
                "name": "Core Router Utama",
                "ip_address": "10.4.11.1",
                "area": AreaZona.ZONA_4,
                "device_type": DeviceType.ROUTER,
            },
            {
                "name": "Core Switch Backbone Depot",
                "ip_address": "10.4.11.2",
                "area": AreaZona.ZONA_4,
                "device_type": DeviceType.SWITCH,
            },
        ]

        for item in initial_devices:
            service = MonitoredService(
                name=item["name"],
                ip_address=item["ip_address"],
                area=item["area"],
                device_type=item["device_type"],
                status="UP",
                response_time_ms=0.0,
                is_maintenance=False
            )
            db.add(service)

        db.commit()
        logger.info("Successfully seeded 8 FT Pengapon target nodes.")

    except Exception as e:
        db.rollback()
        logger.error("Failed to seed database: %s", e)
        raise
    finally:
        db.close()


if __name__ == "__main__":
    seed_data()
