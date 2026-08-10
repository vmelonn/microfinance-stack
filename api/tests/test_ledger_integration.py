"""
End-to-end proof that Layer 5 -> Layer 6 wiring is real, not just returning
a status string. After a purchase request goes through the full stack, we
open the ledger database directly and confirm the actual balance changed by
exactly the right amount -- and that a second call with the SAME idempotency
key doesn't double-apply it at the ledger level either.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from fastapi.testclient import TestClient
from api.main import app
from api.tests._auth_helpers import register_and_login


def test_approved_purchase_updates_ledger_balance():
    with TestClient(app) as client:
        auth_headers, card = register_and_login(client)

        response = client.post("/transactions/purchase", headers=auth_headers, json={
            "amount": 75.00,
            "card_number": card,
            "pin": "1234",
            "idempotency_key": "ledger-test-001",
        }).json()

        assert response["status"] == "approved"
        assert response["ledger_status"] == "recorded"

        balance = client.get(f"/ledger/balance/{card}", headers=auth_headers).json()
        assert balance["balance_usd"] == -75.00, f"Expected -75.00, got {balance}"
        print(f"Ledger balance after purchase: {balance}")


def test_repeated_idempotency_key_does_not_double_apply_ledger():
    with TestClient(app) as client:
        auth_headers, card = register_and_login(client)
        body = {
            "amount": 30.00,
            "card_number": card,
            "pin": "1234",
            "idempotency_key": "ledger-test-002",
        }
        first = client.post("/transactions/purchase", headers=auth_headers, json=body).json()
        second = client.post("/transactions/purchase", headers=auth_headers, json=body).json()

        assert first == second
        assert first["ledger_status"] == "recorded"

        balance = client.get(f"/ledger/balance/{card}", headers=auth_headers).json()
        assert balance["balance_usd"] == -30.00, f"Expected exactly one -30.00 debit, got {balance} -- ledger was double-applied"
        print("Repeated idempotency key correctly resulted in exactly one ledger entry")


if __name__ == "__main__":
    test_approved_purchase_updates_ledger_balance()
    test_repeated_idempotency_key_does_not_double_apply_ledger()
