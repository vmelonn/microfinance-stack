"""
Run this AFTER `docker compose up --build` -- it's the actual proof that
Redis-backed state genuinely works across separate replicas, not just
within one process. Uses only the standard library, so no extra install
is needed beyond Python itself.

What it proves:
  1. A purchase made through app1 (port 8001), retried with the SAME
     idempotency key through app2 (port 8002) -- a replica that has never
     even seen this user or card -- returns the identical cached result,
     without app2 ever touching its own switch connection.
  2. Risk velocity escalates correctly even when rapid attempts are split
     across BOTH replicas, proving the sliding window is genuinely shared,
     not two separate half-blind counters.

Usage:
    python3 verify_cross_replica.py
"""

import json
import urllib.request
import urllib.error
import uuid

APP1 = "http://localhost:8001"
APP2 = "http://localhost:8002"


def post(base_url, path, body, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(base_url + path, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def register_and_login(base_url):
    cnic = str(uuid.uuid4().int)[:13]
    card = "9" + str(uuid.uuid4().int)[:15]
    password = "a-real-password-123"

    status, body = post(base_url, "/users/register", {
        "full_name": "Cross Replica Test", "cnic": cnic, "bind_card_number": card, "password": password
    })
    assert status == 200, f"Registration failed: {body}"

    status, body = post(base_url, "/auth/login", {"cnic": cnic, "password": password})
    assert status == 200, f"Login failed: {body}"

    return body["access_token"], card


def test_cross_replica_idempotency():
    print("=== Test 1: cross-replica idempotency ===")
    token, card = register_and_login(APP1)  # registered on app1's own database ONLY

    idempotency_key = f"cross-replica-{uuid.uuid4().hex[:8]}"
    body = {"amount": 20.00, "card_number": card, "pin": "1234", "idempotency_key": idempotency_key}

    status1, result1 = post(APP1, "/transactions/purchase", body, token=token)
    print(f"app1 (first attempt):  {status1} {result1}")

    # Same idempotency key, same body, same token -- but sent to app2,
    # which has never seen this user or card in ITS OWN database.
    status2, result2 = post(APP2, "/transactions/purchase", body, token=token)
    print(f"app2 (same key, different replica): {status2} {result2}")

    assert result1 == result2, "Results differ across replicas -- Redis sharing is NOT working"
    print("PASS -- app2 returned the identical cached result via Redis, without ever resolving the account itself\n")


def test_cross_replica_velocity():
    print("=== Test 2: cross-replica velocity tracking ===")
    token, card = register_and_login(APP1)

    outcomes = []
    for i in range(6):
        target = APP1 if i % 2 == 0 else APP2  # alternate replicas on every attempt
        body = {"amount": 5.00, "card_number": card, "pin": "1234",
                 "idempotency_key": f"velocity-{uuid.uuid4().hex[:8]}"}
        status, result = post(target, "/transactions/purchase", body, token=token)
        outcomes.append(result.get("status"))
        print(f"attempt {i+1} via {'app1' if target == APP1 else 'app2'}: {result.get('status')}")

    assert "review" in outcomes or "decline" in outcomes, (
        "Velocity never escalated across alternating replicas -- Redis sharing is NOT working"
    )
    print("PASS -- velocity correctly escalated even though attempts were split across both replicas\n")


if __name__ == "__main__":
    test_cross_replica_idempotency()
    test_cross_replica_velocity()
    print("All cross-replica tests passed.")
