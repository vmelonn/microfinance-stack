"""
End-to-end test for Layer 5. Uses FastAPI's TestClient, which triggers the
app's lifespan (startup/shutdown) exactly like a real server would --
so this genuinely exercises every layer underneath: security (PIN
encryption), message building, correlation, and connection, all the way
down to a real socket talking to the host simulator.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from fastapi.testclient import TestClient
from api.main import app
from api.tests._auth_helpers import register_and_login


def test_purchase_approved():
    with TestClient(app) as client:
        auth_headers, card = register_and_login(client)

        response = client.post("/transactions/purchase", headers=auth_headers, json={
            "amount": 50.00,
            "card_number": card,
            "pin": "1234",
            "idempotency_key": "test-key-001",
        })
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "approved"
        assert body["reason"] == "Approved"
        assert body["authorization_id"] == "A18008"
        print("Purchase approved:", body)


def test_idempotency_returns_cached_result_without_reprocessing():
    with TestClient(app) as client:
        auth_headers, card = register_and_login(client)

        first = client.post("/transactions/purchase", headers=auth_headers, json={
            "amount": 25.00,
            "card_number": card,
            "pin": "1234",
            "idempotency_key": "test-key-002",
        }).json()

        received_before = len(client.app.state.simulator.received)

        second = client.post("/transactions/purchase", headers=auth_headers, json={
            "amount": 25.00,
            "card_number": card,
            "pin": "1234",
            "idempotency_key": "test-key-002",   # same key -- should NOT reprocess
        }).json()

        received_after = len(client.app.state.simulator.received)

        assert first == second, "Same idempotency key should return the identical cached result"
        assert received_after == received_before, (
            "The simulator should not have received a second message -- "
            "the idempotency cache should have short-circuited before sending anything"
        )
        print("Idempotency confirmed -- second call did not reach the switch at all")


def test_health_endpoint():
    with TestClient(app) as client:
        response = client.get("/health")
        assert response.status_code == 200
        body = response.json()
        assert body["switch_connected"] is True
        print("Health check OK:", body)


if __name__ == "__main__":
    test_purchase_approved()
    test_idempotency_returns_cached_result_without_reprocessing()
    test_health_endpoint()
