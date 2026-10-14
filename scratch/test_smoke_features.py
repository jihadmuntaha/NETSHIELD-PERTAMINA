"""Smoke test NetShield: CRUD device, bulk CSV, export CSV insiden, navigasi, auth middleware.
Memakai DB SQLite sementara (tidak menyentuh netshield.db)."""
import os, sys, tempfile
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # root project
sys.path.insert(0, ROOT)
os.chdir(ROOT)
tmp_db = os.path.join(tempfile.gettempdir(), "netshield_smoke.db")
if os.path.exists(tmp_db):
    os.remove(tmp_db)
os.environ["DATABASE_URL"] = "sqlite:///" + tmp_db.replace("\\", "/")

from fastapi.testclient import TestClient
from app.database import Base, engine, SessionLocal
import app.routers.auth as auth_mod
from app.main import app
from app.models import Device, IncidentLog

auth_mod.verify_credentials = lambda u, p: True
Base.metadata.create_all(bind=engine)

c = TestClient(app)
ok = lambda cond, msg: print(("PASS " if cond else "FAIL ") + msg)

# --- Proteksi middleware sebelum login ---
r = c.get("/incidents/export/csv", follow_redirects=False)
ok(r.status_code == 303 and r.headers["location"] == "/login", "export/csv terproteksi tanpa login")
r = c.post("/devices/upload-csv", files={"file": ("a.csv", b"name,ip_address\n")}, follow_redirects=False)
ok(r.status_code == 303 and r.headers["location"] == "/login", "upload-csv terproteksi tanpa login")
r = c.post("/api/v1/webhook/whatsapp", data={"sender": "62800", "message": "halo"}, follow_redirects=False)
ok(r.status_code != 303, f"webhook WA tidak lagi di-redirect ke /login (status {r.status_code})")

c.post("/login", data={"username": "tester", "password": "x"}, follow_redirects=False)

# --- Tambah device dengan parent_id kosong / "None" ---
for pid, ip in (("", "10.99.0.1"), ("None", "10.99.0.2"), ("abc", "10.99.0.3")):
    c.post("/devices/add", data={"name": f"Dev-{ip}", "ip_address": ip, "device_type": "ROUTER",
                                 "location_name": "FT Pengapon", "site_type": "FUEL_TERMINAL",
                                 "level": "1", "parent_id": pid}, follow_redirects=False)
db = SessionLocal()
devs = {d.ip_address: d for d in db.query(Device).all()}
ok(all(ip in devs and devs[ip].parent_id is None for ip in ("10.99.0.1", "10.99.0.2", "10.99.0.3")),
   "parent_id '', 'None', 'abc' -> NULL")
d1 = devs["10.99.0.1"]
ok(d1.location_name == "FT Pengapon" and d1.site_type == "FUEL_TERMINAL" and d1.level == 1,
   "location_name/site_type/level tersimpan")

# --- Edit: self-parent ditolak ---
c.post(f"/devices/{d1.id}/edit", data={"name": d1.name, "ip_address": d1.ip_address, "device_type": "ROUTER",
                                        "location_name": "X", "site_type": "FUEL_TERMINAL", "level": "2",
                                        "parent_id": str(d1.id)}, follow_redirects=False)
db.expire_all()
ok(db.get(Device, d1.id).parent_id is None and db.get(Device, d1.id).level == 1, "edit self-parent ditolak")

# --- Bulk CSV: parent_ip mapping + skip duplikat ---
csv_body = ("name,ip_address,device_type,location_name,site_type,level,parent_ip\n"
            "Child-A,10.99.1.2,SWITCH,FT Boyolali,FUEL_TERMINAL,2,10.99.1.1\n"
            "Core-A,10.99.1.1,ROUTER,FT Boyolali,FUEL_TERMINAL,1,\n"
            "Dup,10.99.0.1,ROUTER,FT,FUEL_TERMINAL,1,\n").encode()
c.post("/devices/upload-csv", files={"file": ("bulk.csv", csv_body, "text/csv")}, follow_redirects=False)
db.expire_all()
devs = {d.ip_address: d for d in db.query(Device).all()}
ok("10.99.1.2" in devs and devs["10.99.1.2"].parent_id == devs["10.99.1.1"].id, "parent_ip -> parent_id terpetakan")
ok(sum(1 for d in devs.values() if d.ip_address == "10.99.0.1") == 1 and d1.name == devs["10.99.0.1"].name,
   "IP duplikat dilewati")

# --- Delete ---
r = c.post(f"/devices/{devs['10.99.0.3'].id}/delete", follow_redirects=False)
db.expire_all()
ok(r.status_code == 303 and db.query(Device).filter_by(ip_address="10.99.0.3").first() is None, "delete device")

# --- Insiden & export CSV ---
now = datetime(2026, 10, 10, 8, 0, 0)
db.add_all([
    IncidentLog(device_id=d1.id, severity="CRITICAL", status="RESOLVED", started_at=now,
                resolved_at=now + timedelta(minutes=5), ttr_seconds=300, assigned_to="tester",
                resolution_notes="Kabel diganti"),
    IncidentLog(device_id=d1.id, severity="WARNING", status="NEW", latency_ms=250.5,
                started_at=datetime(2026, 9, 3, 9, 0, 0)),
])
db.commit()
r = c.get("/incidents/export/csv?month=10&year=2026")
lines = r.content.decode("utf-8-sig").strip().splitlines()
ok(lines[0] == "Incident_ID,Timestamp_Down,Timestamp_Up,Duration_Downtime_Sec,Location,Device_Name,IP_Address,"
               "Severity,Trigger_Cause,Status,Handled_By,TTA_Seconds,TTR_Seconds,Resolution_Notes", "header CSV sesuai")
ok(len(lines) == 2 and "Host Unreachable (up=0)" in lines[1] and ",300," in lines[1], "filter bulan + trigger CRITICAL")
ok("NetShield_Incidents_Report_" in r.headers["content-disposition"], "nama file sesuai")
r = c.get("/incidents/export/csv?start_date=2026-09-01&end_date=2026-09-30")
body = r.content.decode("utf-8-sig")
ok("High Latency (RTT: 250.5ms)" in body and "STILL DOWN" in body, "filter rentang hari + trigger WARNING")

# --- Halaman & navigasi ---
r = c.get("/incidents")
ok(r.status_code == 200 and "STILL DOWN" in r.text and "form-export-month" in r.text, "halaman incidents render")
r = c.get("/devices")
ok(r.status_code == 200 and "uploadCsvModal" in r.text and "editDeviceModal" in r.text, "halaman devices render")
ok('href="/topology"' not in r.text and "Incident Center" in r.text and "/logout" in r.text, "navbar tanpa Topology")
db.close()

