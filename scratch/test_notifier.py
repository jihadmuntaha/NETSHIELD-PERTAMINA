import asyncio
import os
import sys
from datetime import datetime

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# Adjust sys.path to import app modules
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.services.notifier import (
    send_incident_alert,
    send_recovery_alert,
    _format_incident_message,
    _format_recovery_message,
    _resolve_target_for_device,
)
from app.models.monitoring import AreaZona, DeviceType


class MockDevice:
    def __init__(self, name, ip, area, response_time_ms=5.2):
        self.name = name
        self.ip_address = ip
        self.area = area
        self.device_type = DeviceType.ROUTER
        self.response_time_ms = response_time_ms


class MockIncident:
    def __init__(self, severity="HIGH", created_at=None, resolved_at=None):
        self.severity = severity
        self.created_at = created_at or datetime.now()
        self.resolved_at = resolved_at


async def main():
    print("[TEST 1] Dynamic Target Mapping per Zone")
    dev_z1 = MockDevice("Router Gate Utama", "192.168.1.1", AreaZona.ZONA_1)
    dev_z2 = MockDevice("Switch Tangki Timbun", "192.168.2.1", AreaZona.ZONA_2)
    dev_z3 = MockDevice("Flow Meter Bay 1", "192.168.3.1", AreaZona.ZONA_3)
    dev_z4 = MockDevice("Core Switch IT", "192.168.4.1", AreaZona.ZONA_4)

    t1 = _resolve_target_for_device(dev_z1)
    t2 = _resolve_target_for_device(dev_z2)
    t3 = _resolve_target_for_device(dev_z3)
    t4 = _resolve_target_for_device(dev_z4)

    print(f"Zona 1 Target: {t1}")
    print(f"Zona 2 Target: {t2}")
    print(f"Zona 3 Target: {t3}")
    print(f"Zona 4 Target: {t4}")

    assert t1 != "", "Target Zona 1 must not be empty"

    print("\n[TEST 2] Message Format Formatting Check")
    inc_down = MockIncident(severity="HIGH")
    inc_up = MockIncident(resolved_at=datetime.now())

    msg_down = _format_incident_message(dev_z1, inc_down)
    msg_up = _format_recovery_message(dev_z1, inc_up, latency=1.4)

    print("=== DOWN MESSAGE ===")
    print(msg_down)
    print("=== RECOVERY MESSAGE ===")
    print(msg_up)

    assert "🚨 *PERTAMINA NETSHIELD - NETWORK ALERT* 🚨" in msg_down
    assert "Status:* DOWN (Unreachable)" in msg_down
    assert "Severity:* HIGH" in msg_down
    assert "✅ *PERTAMINA NETSHIELD - RESOLVED ALERT* ✅" in msg_up
    assert "Status:* UP (Recovered)" in msg_up
    assert "Latensi:* 1.4 ms" in msg_up

    print("\n[TEST 3] Calling send_incident_alert and send_recovery_alert (httpx execution test)")
    res_down = await send_incident_alert(dev_z1, inc_down)
    res_up = await send_recovery_alert(dev_z1, inc_up, latency=1.4)
    print(f"Send Alert Result: {res_down}")
    print(f"Send Recovery Result: {res_up}")

    print("\n[ALL NOTIFIER TESTS PASSED SUCCESSFULY]")


if __name__ == "__main__":
    asyncio.run(main())
