"""Smoke test NOC Toolkit: engine ping + endpoint /toolkit & /toolkit/api/ping."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.utils.ping import (  # noqa: E402
    InvalidPingTarget, execute_icmp_ping, normalize_ping_target, parse_avg_latency,
)

# ---- Parser ----
assert parse_avg_latency("Minimum = 1ms, Maximum = 3ms, Average = 2ms") == 2.0
assert parse_avg_latency("Minimum = 10ms, Maksimum = 30ms, Rata-rata = 21ms") == 21.0
assert parse_avg_latency("rtt min/avg/max/mdev = 0.045/0.056/0.067/0.010 ms") == 0.056
assert parse_avg_latency("round-trip min/avg/max = 1.1/2.2/3.3 ms") == 2.2
assert parse_avg_latency("Reply from 1.1.1.1: bytes=32 time=10ms TTL=57\nReply from 1.1.1.1: bytes=32 time=20ms TTL=57") == 15.0
assert parse_avg_latency("nothing") is None
print("parser OK")

# ---- Normalizer ----
assert normalize_ping_target(" 10.4.12.1 ") == "10.4.12.1"
assert normalize_ping_target("127.0.0.1:8000") == "127.0.0.1"
assert normalize_ping_target("https://pertamina.com/x") == "pertamina.com"
assert normalize_ping_target("[::1]") == "::1"
for bad in ["", "-t 8.8.8.8", "8.8.8.8; rm -rf /", "a b", "$(whoami)", "--help"]:
    try:
        normalize_ping_target(bad)
        raise AssertionError(f"should reject {bad!r}")
    except InvalidPingTarget:
        pass
print("normalizer OK")

# ---- Engine (real ping) ----
up, avg, raw = execute_icmp_ping("127.0.0.1", 2)
print("localhost ->", up, avg)
assert up and avg is not None, raw
down, avg2, raw2 = execute_icmp_ping("192.0.2.1", 1, 500)  # TEST-NET-1, tidak routable
print("TEST-NET  ->", down, avg2)
assert not down and avg2 is None
nx = execute_icmp_ping("host-tidak-ada.invalid", 1)
print("NXDOMAIN  ->", nx[0], "|", nx[2].splitlines()[0] if nx[2] else "")
assert not nx[0]

# ---- Endpoint ----
from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402
import app.routers.auth as auth_router  # noqa: E402

auth_router.verify_credentials = lambda u, p: True  # test-only: bypass credential store

if True:
    c = TestClient(app)  # tanpa context manager -> startup poller tidak dijalankan
    r = c.get("/toolkit", follow_redirects=False)
    assert r.status_code == 303 and "/login" in r.headers["location"], r.status_code

    lr = c.post("/login", data={"username": "tester", "password": "x"}, follow_redirects=False)
    login_ok = lr.status_code == 303
    print("login:", login_ok)
    if login_ok:
        r = c.get("/toolkit")
        assert r.status_code == 200 and "Live ICMP Ping Probe" in r.text
        r = c.post("/toolkit/api/ping", json={"target_ip": "127.0.0.1", "count": 1})
        j = r.json()
        print("api ->", r.status_code, {k: j[k] for k in ("target", "is_alive", "avg_latency_ms", "command")})
        assert r.status_code == 200 and j["is_alive"] is True
        r = c.post("/toolkit/api/ping", json={"target_ip": "-n 1 x", "count": 1})
        assert r.status_code == 400, r.status_code
        r = c.post("/toolkit/api/ping", json={"target_ip": "127.0.0.1", "count": 99})
        assert r.status_code == 422, r.status_code
        r = c.post("/toolkit/api/ping", data={"target_ip": "127.0.0.1", "count": "1"})
        assert r.status_code == 200 and r.json()["is_alive"], r.text
        r = c.get("/portal")
        assert 'href="/toolkit" id="btn-ws-toolkit" class="ws-btn">' in r.text
        print("endpoint OK")
print("ALL OK")
