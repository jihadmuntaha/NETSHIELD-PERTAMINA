import logging
from app.database import engine, SessionLocal, Base
from app.models.device import Device

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("seed_topology")


def seed_topology():
    """
    Seeder data topologi awal hierarki berbasis parent_id.
    Idempotent: Hanya menambahkan device yang belum ada di database,
    dan menyesuaikan parent_id relasi uplink secara tepat.
    """
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()

    try:
        topology_devices = [
            {
                "name": "ISP-Gateway",
                "ip_address": "10.10.0.1",
                "device_type": "gateway",
                "parent_name": None,
                "zone": "Fuel Terminal",
            },
            {
                "name": "Core-Firewall",
                "ip_address": "192.168.1.1",
                "device_type": "core",
                "parent_name": "ISP-Gateway",
                "zone": "Server Room",
            },
            {
                "name": "SW-Distribution-Zone1",
                "ip_address": "192.168.10.1",
                "device_type": "distribution",
                "parent_name": "Core-Firewall",
                "zone": "Zona 1",
            },
            {
                "name": "Target-Server-8080",
                "ip_address": "127.0.0.1:8080",
                "device_type": "access",
                "parent_name": "SW-Distribution-Zone1",
                "zone": "Zona 1",
            },
        ]

        name_to_device = {}

        for item in topology_devices:
            dev = db.query(Device).filter(Device.name == item["name"]).first()
            parent_id = None
            if item["parent_name"]:
                parent_dev = name_to_device.get(item["parent_name"]) or db.query(Device).filter(Device.name == item["parent_name"]).first()
                if parent_dev:
                    parent_id = parent_dev.id

            if not dev:
                dev = Device(
                    name=item["name"],
                    ip_address=item["ip_address"],
                    device_type=item["device_type"],
                    parent_id=parent_id,
                    zone=item["zone"],
                )
                db.add(dev)
                db.commit()
                db.refresh(dev)
                logger.info("Created topology device: %s (id=%d, parent_id=%s)", dev.name, dev.id, dev.parent_id)
            else:
                if parent_id and dev.parent_id != parent_id:
                    dev.parent_id = parent_id
                    db.commit()
                    db.refresh(dev)
                logger.info("Topology device already exists: %s (id=%d, parent_id=%s)", dev.name, dev.id, dev.parent_id)

            name_to_device[dev.name] = dev

        logger.info("Topology database seeding completed successfully.")

    except Exception as e:
        db.rollback()
        logger.error("Failed to seed topology data: %s", e)
        raise
    finally:
        db.close()


if __name__ == "__main__":
    seed_topology()
