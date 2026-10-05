import os
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app, follow_redirects=False)

def run_tests():
    print("Testing unauthenticated redirect...")
    res = client.get("/noc")
    assert res.status_code == 303, f"Expected 303, got {res.status_code}"
    assert res.headers["location"] == "/login", f"Expected /login, got {res.headers.get('location')}"
    print("-> PASS: Unauthenticated redirect to /login")

    print("Testing whitelist /webhook...")
    res = client.post("/webhook/whatsapp", data={"Body": "STATUS", "From": "whatsapp:+628123456789"})
    assert res.status_code != 303, f"Expected not 303, got {res.status_code}"
    print("-> PASS: Whitelisted endpoint bypassed auth")

    print("Testing GET /login render...")
    res = client.get("/login")
    assert res.status_code == 200
    assert "PERTAMINA" in res.text and "NETSHIELD" in res.text
    assert "ID OPERATOR" in res.text
    print("-> PASS: GET /login rendered successfully")

    print("Testing POST /login invalid credentials...")
    res = client.post("/login", data={"username": "wrong", "password": "bad"})
    assert res.status_code == 400
    assert "ID Operator atau Password tidak valid!" in res.text
    print("-> PASS: Invalid login returned 400 with error message")

    print("Testing POST /login valid credentials & session...")
    res = client.post("/login", data={"username": "admin", "password": "password"})
    assert res.status_code == 303
    assert res.headers["location"] == "/"
    
    # Store session cookie
    session_cookie = res.cookies.get("session")
    assert session_cookie is not None, "Session cookie not found!"
    
    # Test protected route /noc with session cookie
    res_noc = client.get("/noc", cookies={"session": session_cookie})
    assert res_noc.status_code == 200
    assert "Operator:" in res_noc.text
    assert "admin" in res_noc.text
    print("-> PASS: Authenticated session accessed protected route /noc")

    print("Testing GET /logout...")
    res_logout = client.get("/logout", cookies={"session": session_cookie})
    assert res_logout.status_code == 303
    assert res_logout.headers["location"] == "/login"
    print("-> PASS: Logout cleared session and redirected to /login")

    print("\nALL AUTHENTICATION TESTS PASSED PERFECTLY!")

if __name__ == "__main__":
    run_tests()
