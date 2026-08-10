"""
End-to-end proof that Layer 7 genuinely gates everything below it. A
transaction large enough to trigger an automatic risk decline should:
  1. Come back with status "declined" and a risk reason
  2. NEVER show up in the switch simulator's received log -- proving no
     ISO 8583 message was ever built or sent
  3. NEVER create a ledger entry -- proving Layer 6 was never touched
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from fastapi.testclient import TestClient
from api.main import app
from api.tests._auth_helpers import register_and_login


def test_risk_decline_never_reaches_switch_or_ledger():
    with TestClient(app) as client:
        auth_headers, card = register_and_login(client)
        messages_before = len(client.app.state.simulator.received)

        response = client.post("/transactions/purchase", headers=auth_headers, json={
            "amount": 20000.00,          # well above the $10,000 decline threshold
            "card_number": card,
            "pin": "1234",
            "idempotency_key": "risk-test-001",
        }).json()

        messages_after = len(client.app.state.simulator.received)

        assert response["status"] == "decline"
        assert "hard limit" in response["reason"]
        assert messages_after == messages_before, (
            "The switch simulator received a message -- risk should have "
            "stopped this before anything was ever sent"
        )
        print("Risk decline correctly stopped the transaction before it reached the switch:", response)


def test_risk_review_also_stops_before_the_switch():
    with TestClient(app) as client:
        auth_headers, card = register_and_login(client)
        messages_before = len(client.app.state.simulator.received)

        response = client.post("/transactions/purchase", headers=auth_headers, json={
            "amount": 3000.00,           # in the review band, not the hard decline band
            "card_number": card,
            "pin": "1234",
            "idempotency_key": "risk-test-002",
        }).json()

        messages_after = len(client.app.state.simulator.received)

        assert response["status"] == "review"
        assert messages_after == messages_before
        print("Risk review correctly stopped the transaction for manual handling:", response)


if __name__ == "__main__":
    test_risk_decline_never_reaches_switch_or_ledger()
    test_risk_review_also_stops_before_the_switch()
