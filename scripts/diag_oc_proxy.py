"""Diagnose the curl-200 / urllib-401 asymmetry on OC /v1/health.

If a proxy env var routes urllib through a proxy that lacks the OC token,
the health check 401s while curl (honoring no_proxy for localhost) gets
200. This decides whether the GitHub->OC 302 can light up live.
"""
import json
import os
import sys
import urllib.request

sys.path.insert(0, ".")
TOKEN = "runttest123"
URL = "http://127.0.0.1:3001/v1/health"

print("=== proxy-related env vars ===")
for k in sorted(os.environ):
    if "proxy" in k.lower():
        v = os.environ[k]
        print(f"  {k} = {v[:80]}{'...' if len(v) > 80 else ''}")

def probe(label, opener=None):
    req = urllib.request.Request(URL, headers={"Authorization": f"Bearer {TOKEN}"})
    try:
        with (opener or urllib.request).urlopen(req, timeout=3.0) as r:
            body = json.loads(r.read().decode())
            print(f"{label}: HTTP {r.status} ok={body.get('data', {}).get('ok')}")
            return r.status
    except urllib.error.HTTPError as e:
        print(f"{label}: HTTP {e.code} {e.reason}")
        return e.code
    except Exception as e:
        print(f"{label}: {type(e).__name__}: {e}")
        return -1

print("\n=== urllib probes (same URL + token that curl gets 200 on) ===")
probe("default urllib proxy handling")

# Force NO proxy (empty ProxyHandler -> uses only no_proxy/localhost bypass).
no_proxy = urllib.request.build_opener(urllib.request.ProxyHandler({}))
probe("urllib with ProxyHandler({}) [no proxy]", no_proxy)

print("\n=== ground truth via curl (same call, subprocess) ===")
import subprocess
out = subprocess.run(
    ["curl", "-s", "-o", "dev/null", "-w", "%{http_code}",
     "-H", f"Authorization: Bearer {TOKEN}", URL],
    capture_output=True, text=True,
)
print(f"curl: HTTP {out.stdout}")
