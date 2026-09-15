import asyncio
import os
import sys
from datetime import datetime

# Adjust sys.path to import app modules
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.database import Base, SessionLocal
from app.models.monitoring import MonitoredService, IncidentLog, AreaZona, DeviceType
from app.routers.webhook import ACK_REGEX, whatsapp_webhook
from app.services.notifier import format_ack_confirmation


def test_regex_parsing():
    print("=== TESTING REGEX PATTERN ===")
    test_cases = [
        ("ack 104 Sedang cek switch", 104, "Sedang cek switch"),
        ("ack#104 OTW lokasi", 104, "OTW lokasi"),
        ("ack 104", 104, "Dikonfirmasi via WhatsApp"),
        ("ACK #104: Penanganan perangkat", 104, "Penanganan perangkat"),
        ("  ACK#99   ", 99, "Dikonfirmasi via WhatsApp"),
        ("hello world", None, None),
    ]

    for msg, expected_id, expected_note in test_cases:
        match = ACK_REGEX.match(msg)
        if expected_id is None:
            assert match is None, f"Expected no match for '{msg}'"
            print(f"[PASS] Ignored non-ACK message: '{msg}'")
        else:
            assert match is not None, f"Expected match for '{msg}'"
            g_id, g_note = match.groups()
            inc_id = int(g_id)
            cleaned_note = (g_note or "").strip().lstrip(":").strip()
            note = cleaned_note if cleaned_note else "Dikonfirmasi via WhatsApp"
            assert inc_id == expected_id, f"Expected ID {expected_id}, got {inc_id}"
            assert note == expected_note, f"Expected note '{expected_note}', got '{note}'"
            print(f"[PASS] Parsed '{msg}' -> ID: #{inc_id}, Note: '{note}'")



def test_format_confirmation():
    print("\n=== TESTING CONFIRMATION MESSAGE FORMAT ===")
    msg = format_ack_confirmation(
        incident_id=104,
        device_name="Router Main Gate",
        ip_address="192.168.1.1",
        zone_name="Zona 1 - Gate & Main Office",
        acknowledged_by="Ahmad Teknisi",
        acknowledged_at_formatted="15/09/2026 10:13:35",
        ack_message="Sudah diperbaiki",
    )
    print(msg)
    assert "✅ *PERTAMINA NETSHIELD - ACK CONFIRMED* ✅" in msg
    assert "*ID Insiden:* #104" in msg
    assert "*Perangkat:* Router Main Gate (192.168.1.1)" in msg
    assert "*Zona:* Zona 1 - Gate & Main Office" in msg
    assert "*PIC:* Ahmad Teknisi" in msg
    assert "*Waktu ACK:* 15/09/2026 10:13:35 WIB" in msg
    assert "*Catatan:* Sudah diperbaiki" in msg
    assert "_Status di Dashboard NOC telah diperbarui._" in msg
    print("[PASS] Confirmation message matches specified layout exactly.")


async def test_db_scenarios():
    print("\n=== TESTING DB SCENARIOS (KASUS A, B, C, D) ===")

    # Use in-memory SQLite engine with StaticPool for test isolation across sessions
    from sqlalchemy.pool import StaticPool
    test_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool
    )
    TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)
    Base.metadata.create_all(bind=test_engine)


    # Patch SessionLocal in webhook module for test duration
    import app.routers.webhook as webhook_mod
    original_session_local = webhook_mod.SessionLocal
    webhook_mod.SessionLocal = TestingSession

    db = TestingSession()
    try:
        # Create service fixture
        svc = MonitoredService(
            name="Switch Tangki 1",
            ip_address="10.10.2.1",
            area=AreaZona.ZONA_2,
            device_type=DeviceType.SWITCH,
            status="DOWN"
        )
        db.add(svc)
        db.commit()
        db.refresh(svc)


        # Incident 1: NEW (for Kasus D)
        inc_new = IncidentLog(
            service_id=svc.id,
            severity="HIGH",
            status="NEW"
        )
        # Incident 2: ACKNOWLEDGED (for Kasus B)
        inc_ack = IncidentLog(
            service_id=svc.id,
            severity="HIGH",
            status="ACKNOWLEDGED",
            ack_by="Budi NOC",
            ack_at=datetime.utcnow()
        )
        # Incident 3: RESOLVED (for Kasus C)
        inc_res = IncidentLog(
            service_id=svc.id,
            severity="WARNING",
            status="RESOLVED"
        )

        db.add_all([inc_new, inc_ack, inc_res])
        db.commit()
        db.refresh(inc_new)
        db.refresh(inc_ack)
        db.refresh(inc_res)

        new_id = inc_new.id
        ack_id = inc_ack.id
        res_id = inc_res.id
        non_existent_id = 99999
    finally:
        db.close()

    # Mock Request class for calling whatsapp_webhook
    class MockRequest:
        def __init__(self, json_data=None, form_data=None, content_type="application/json"):
            self._json = json_data or {}
            self._form = form_data or {}
            self.headers = {"content-type": content_type}
            self.client = type("Client", (), {"host": "127.0.0.1"})()

        async def json(self):
            return self._json

        async def form(self):
            return self._form

    # 1. Kasus A: Insiden Tidak Ditemukan
    req_a = MockRequest(json_data={"sender": "628123456789", "message": f"ACK {non_existent_id}", "name": "Budi"})
    res_a = await whatsapp_webhook(req_a)
    assert res_a == {"status": "ok"}, f"Kasus A must return status ok, got {res_a}"
    print(f"[PASS] Kasus A (Not Found #{non_existent_id}) -> HTTP 200 {res_a}")

    # 2. Kasus B: Insiden Sudah Di-ACK
    req_b = MockRequest(json_data={"sender": "628123456789", "message": f"ACK #{ack_id} Cek ulang", "name": "Budi"})
    res_b = await whatsapp_webhook(req_b)
    assert res_b == {"status": "ok"}, f"Kasus B must return status ok, got {res_b}"
    print(f"[PASS] Kasus B (Already ACKed #{ack_id}) -> HTTP 200 {res_b}")

    # 3. Kasus C: Insiden Sudah RESOLVED
    req_c = MockRequest(json_data={"sender": "628123456789", "message": f"ACK {res_id}", "name": "Budi"})
    res_c = await whatsapp_webhook(req_c)
    assert res_c == {"status": "ok"}, f"Kasus C must return status ok, got {res_c}"
    print(f"[PASS] Kasus C (Already RESOLVED #{res_id}) -> HTTP 200 {res_c}")

    # 4. Kasus D: Valid ACK
    req_d = MockRequest(json_data={"sender": "628123456789", "message": f"ACK #{new_id} OTW lokasi tangki 1", "name": "Jihad Muntaha"})
    print(f"Sending req_d for incident id: #{new_id}")
    res_d = await whatsapp_webhook(req_d)
    print(f"res_d result: {res_d}")

    # Verify DB mutation for Kasus D
    db_check = TestingSession()
    try:
        updated_inc = db_check.query(IncidentLog).filter(IncidentLog.id == new_id).first()
        print(f"DB Check for #{new_id}: status={updated_inc.status}, ack_by={updated_inc.acknowledged_by}")
        assert updated_inc.status == "ACKNOWLEDGED", f"Status should be ACKNOWLEDGED, got {updated_inc.status}"

        assert updated_inc.is_acknowledged is True
        assert updated_inc.acknowledged_by == "Jihad Muntaha"
        assert updated_inc.ack_message == "OTW lokasi tangki 1"
        assert updated_inc.acknowledged_at is not None
        print(f"[PASS] Kasus D (Valid ACK #{new_id}) successfully updated DB!")
        print(f"       -> Status: {updated_inc.status}")
        print(f"       -> PIC: {updated_inc.acknowledged_by}")
        print(f"       -> Catatan: {updated_inc.ack_message}")
        print(f"       -> Waktu ACK: {updated_inc.acknowledged_at}")
    finally:
        db_check.close()
        webhook_mod.SessionLocal = original_session_local

    print("\n[ALL WEBHOOK VERIFICATION TESTS PASSED SUCCESSFULLY]")



if __name__ == "__main__":
    test_regex_parsing()
    test_format_confirmation()
    asyncio.run(test_db_scenarios())
