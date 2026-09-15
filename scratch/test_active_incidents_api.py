import sys
import os

# Adjust sys.path to import app modules
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from datetime import datetime
from app.database import SessionLocal, Base, engine
from app.models.monitoring import MonitoredService, IncidentLog, AreaZona, DeviceType
from app.routers.incidents import get_active_incidents

def test_active_incidents_endpoint():
    print("=== TESTING GET /api/v1/incidents/active ENDPOINT ===")
    db = SessionLocal()
    try:
        # Fetch active incidents
        active_list = get_active_incidents(db)
        print(f"Total active incidents found: {len(active_list)}")
        for inc in active_list:
            print(f" - ID: #{inc['id']} | Device: {inc['device_name']} ({inc['ip_address']}) | Status: {inc['status']} | ACK: {inc['is_acknowledged']} | PIC: {inc['acknowledged_by']} ({inc['acknowledged_at']})")
            assert inc['status'] != "RESOLVED", f"Resolved incident #{inc['id']} should not be in active list"
            assert "id" in inc
            assert "device_name" in inc
            assert "ip_address" in inc
            assert "zone" in inc
            assert "severity" in inc
            assert "is_acknowledged" in inc
            assert "acknowledged_by" in inc
            assert "acknowledged_at" in inc
            assert "ack_message" in inc
            assert "created_at" in inc
        print("[PASS] Active incidents API endpoint returned valid active items!")
    finally:
        db.close()

if __name__ == "__main__":
    test_active_incidents_endpoint()
