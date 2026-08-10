import sys
sys.path.insert(0, ".")
from server import app
from fastapi.testclient import TestClient

c = TestClient(app)
passed = 0
failed = 0


def check(name, actual, expected):
    global passed, failed
    if actual == expected:
        passed += 1
    else:
        failed += 1
        print(f"  FAIL  {name} -- expected {expected}, got {actual}")


check("GET /", c.get("/").status_code, 200)
check("GET /api/agents", c.get("/api/agents").status_code, 200)
check("POST /api/agent/command", c.post("/api/agent/command", json={"command": "test"}).status_code, 200)
check("GET /api/integrations", c.get("/api/integrations").status_code, 200)
check("GET /api/settings", c.get("/api/settings").status_code, 200)
check("GET /api/gateway/status", c.get("/api/gateway/status").status_code, 200)
check("GET /api/v1/status", c.get("/api/v1/status").status_code, 200)

print(f"{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
