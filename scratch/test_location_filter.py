"""Smoke test segmentasi lokasi / unit operasi JBT. Memakai DB SQLite sementara (tidak menyentuh netshield.db)."""
import os, sys, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
tmp_db = os.path.join(tempfile.gettempdir(), "netshield_location.db")
if os.path.exists(tmp_db):
    os.remove(tmp_db)
os.environ["DATABASE_URL"] = "sqlite:///" + tmp_db.replace("\\", "/")

from fastapi.testclient import TestClient
import app.routers.auth as auth_mod
from app.main import app
from app.database import SessionLocal
from app.models import Device
from app.core.locations import classify_location, resolve_location_filter
from seed_pertamina_jbt import seed_database, ensure_jbt_demo_units

auth_mod.verify_credentials = lambda u, p: True
seed_database()

fails = 0
def ok(cond, msg):
    global fails
    fails += 0 if cond else 1
    print(("PASS " if cond else "FAIL ") + msg)

# --- Classifier ---
ok(classify_location("FT Pengapon Semarang").key == "IT Semarang", "FT Pengapon -> IT Semarang")
ok(classify_location("Regional HQ Semarang").key == "HQ Semarang", "Regional HQ Semarang -> HQ Semarang")
ok(classify_location("Fuel Terminal Tegal").key == "FT Tegal", "Fuel Terminal Tegal -> FT Tegal")
ok(classify_location("DPPU Adi Sumarmo").key == "AFT JBT", "DPPU Adi Sumarmo -> AFT JBT")
ok(classify_location("Gudang Custom") is None, "lokasi custom tidak dipaksa masuk unit")
ok(resolve_location_filter("ALL", []) is None and resolve_location_filter("", []) is None, "ALL / kosong -> tanpa filter")

# --- Seeder idempotent ---
ok(ensure_jbt_demo_units() == 0, "ensure_jbt_demo_units idempotent (FT Tegal sudah ada dari seed)")
db = SessionLocal()
tegal = db.query(Device).filter(Device.location_name == "FT Tegal").all()
ok(sorted(d.name for d in tegal) == sorted(["FT-TGL-Edge-Router", "FT-TGL-Switch-Tangki", "FT-TGL-ATG-01", "FT-TGL-CCTV-Gateway"]),
   f"4 perangkat FT Tegal ter-seed ({len(tegal)})")
edge = next(d for d in tegal if d.name == "FT-TGL-Edge-Router")
ok(edge.parent is not None and edge.parent.ip_address == "10.10.0.1", "FT-TGL-Edge-Router uplink ke HQ Core")
total = db.query(Device).count()
db.close()

c = TestClient(app)
c.post("/login", data={"username": "tester", "password": "x"}, follow_redirects=False)

# --- API ---
r = c.get("/api/devices", params={"location": "FT Tegal"})
ok(r.status_code == 200 and len(r.json()) == 4 and all(d["location_name"] == "FT Tegal" for d in r.json()), "GET /api/devices?location=FT Tegal -> 4")
r = c.get("/api/devices", params={"location": "ALL"})
ok(len(r.json()) == total, f"GET /api/devices?location=ALL -> {total}")
r = c.get("/api/devices", params={"location": "IT Semarang"})
ok(len(r.json()) == 11, f"IT Semarang menggabungkan IT Semarang + FT Pengapon ({len(r.json())})")
r = c.get("/api/devices", params={"location": "FT Maos"})
ok(r.status_code == 200 and r.json() == [], "unit tanpa perangkat -> list kosong")
r = c.get("/api/devices", params={"location": "tegal"})
ok(len(r.json()) == 4, "pencarian case-insensitive 'tegal'")
r = c.get("/api/devices/locations")
units = {u["key"]: u for u in r.json()["units"]}
ok(units["FT Tegal"]["total"] == 4 and units["FT Rewulu"]["down"] == 1 and units["FT Boyolali"]["warning"] == 1,
   "ringkasan per unit (total / down / warning) benar")
ok(r.json()["region"]["total"] == total, "ringkasan region = total perangkat")

# --- Web page ---
r = c.get("/devices", params={"location": "FT Tegal"})
html = r.text
ok(r.status_code == 200, "GET /devices?location=FT Tegal -> 200")
ok("FT-TGL-ATG-01" in html and "HQ-Core-Router-WAN</div>" not in html, "tabel hanya berisi perangkat FT Tegal")
ok("(4 Perangkat Terdaftar)" in html, "header aktif menampilkan jumlah perangkat")
ok('id="btn-reset-location"' in html, "tombol Reset tampil saat difilter")
ok(html.count("is-active") >= 1 and 'data-unit="FT Tegal"' in html, "kartu FT Tegal tersedia & aktif")
r = c.get("/devices")
ok(r.status_code == 200 and 'id="btn-reset-location"' not in r.text and f"({total} Perangkat Terdaftar)" in r.text, "tanpa filter -> semua perangkat, tanpa tombol Reset")
r = c.get("/devices", params={"location": "FT Lomanis"})
ok(r.status_code == 200 and "Belum ada perangkat terdaftar di" in r.text, "unit kosong -> empty state")

# --- Redirect mempertahankan filter ---
r = c.post("/devices/add", data={"name": "FT-TGL-AP-02", "ip_address": "10.60.5.2", "device_type": "SWITCH",
                                 "location_name": "FT Tegal", "site_type": "FUEL_TERMINAL", "level": "3", "parent_id": ""},
           headers={"referer": "http://testserver/devices?location=FT+Tegal"}, follow_redirects=False)
ok(r.status_code == 303 and r.headers["location"] == "/devices?location=FT+Tegal", f"redirect setelah tambah tetap di filter ({r.headers.get('location')})")

print("\nSELESAI:", "SEMUA PASS" if fails == 0 else f"{fails} FAIL")
