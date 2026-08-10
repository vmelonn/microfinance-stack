"""
End-to-end proof of the auth feature, driven against the real FastAPI app --
register, login, protected purchase, and critically, the ownership check
that stops one user's valid token from touching another user's card.

api/main.py uses a PERSISTENT ledger.db (by design, for interactive
testing), not a fresh one per run -- so every identifier here (CNIC, card
number) is generated fresh per test run via a random suffix, so this file
is safely re-runnable without colliding with a previous run's leftover
registrations.
"""

import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from fastapi.testclient import TestClient
from api.main import app


def _unique_cnic() -> str:
    return str(uuid.uuid4().int)[:13]


def _unique_card() -> str:
    return "9" + str(uuid.uuid4().int)[:15]


def _register(client, cnic, card, name="Test User", password="hunter22345"):
    return client.post("/users/register", json={
        "full_name": name, "cnic": cnic, "bind_card_number": card, "password": password,
    })


def test_purchase_without_token_is_rejected():
    with TestClient(app) as client:
        r = client.post("/transactions/purchase", json={
            "amount": 5.00, "card_number": _unique_card(), "pin": "1234",
            "idempotency_key": f"auth-test-noauth-{uuid.uuid4().hex[:8]}",
        })
        assert r.status_code == 401
        print("Purchase with no Authorization header correctly rejected:", r.json())


def test_register_login_and_authenticated_purchase():
    with TestClient(app) as client:
        cnic, card = _unique_cnic(), _unique_card()
        password = "a-real-password-1"
        reg = _register(client, cnic=cnic, card=card, password=password)
        assert reg.status_code == 200, reg.text

        login = client.post("/auth/login", json={"cnic": cnic, "password": password})
        assert login.status_code == 200, login.text
        token = login.json()["access_token"]
        print("Login issued a token:", token[:30], "...")

        purchase = client.post("/transactions/purchase",
            headers={"Authorization": f"Bearer {token}"},
            json={"amount": 5.00, "card_number": card, "pin": "1234",
                  "idempotency_key": f"auth-test-{uuid.uuid4().hex[:8]}"},
        )
        assert purchase.status_code == 200
        print("Authenticated purchase result:", purchase.json())


def test_wrong_password_rejected():
    with TestClient(app) as client:
        cnic = _unique_cnic()
        _register(client, cnic=cnic, card=_unique_card(), password="the-real-password")
        r = client.post("/auth/login", json={"cnic": cnic, "password": "totally-wrong"})
        assert r.status_code == 401
        print("Wrong password correctly rejected:", r.json())


def test_cannot_use_someone_elses_card_with_your_own_token():
    """The real payoff: a valid token for user A must not be able to touch user B's card."""
    with TestClient(app) as client:
        cnic_a, card_a = _unique_cnic(), _unique_card()
        cnic_b, card_b = _unique_cnic(), _unique_card()
        pw_a = "password-a-123"
        _register(client, cnic=cnic_a, card=card_a, name="User A", password=pw_a)
        _register(client, cnic=cnic_b, card=card_b, name="User B", password="password-b-123")

        login_a = client.post("/auth/login", json={"cnic": cnic_a, "password": pw_a})
        token_a = login_a.json()["access_token"]

        # User A's valid token, but attempting to charge User B's card
        r = client.post("/transactions/purchase",
            headers={"Authorization": f"Bearer {token_a}"},
            json={"amount": 5.00, "card_number": card_b, "pin": "1234",
                  "idempotency_key": f"auth-test-crossuser-{uuid.uuid4().hex[:8]}"},
        )
        assert r.status_code == 403, r.text
        print("User A's token correctly blocked from using User B's card:", r.json())


def test_migrated_user_must_set_password_before_login():
    """A user that existed before password_hash was added (empty hash) can't log in until they set one."""
    with TestClient(app) as client:
        import sqlite3
        cnic = _unique_cnic()
        user_id = f"usr_legacy_test_{uuid.uuid4().hex[:8]}"
        conn = sqlite3.connect(client.app.state.ledger_db_path)
        conn.execute(
            "INSERT INTO users (user_id, full_name, cnic, password_hash) VALUES (?, ?, ?, '')",
            (user_id, "Legacy User", cnic),
        )
        conn.commit()
        conn.close()

        blocked = client.post("/auth/login", json={"cnic": cnic, "password": "anything"})
        assert blocked.status_code == 401

        set_pw = client.post("/auth/set-password", json={"cnic": cnic, "new_password": "new-real-password"})
        assert set_pw.status_code == 200, set_pw.text

        now_works = client.post("/auth/login", json={"cnic": cnic, "password": "new-real-password"})
        assert now_works.status_code == 200

        # And set-password can't be used a second time to silently reset it again
        blocked_again = client.post("/auth/set-password", json={"cnic": cnic, "new_password": "hijacked"})
        assert blocked_again.status_code == 400
        print("Legacy user: blocked -> set-password -> login works -> second set-password blocked. All correct.")


if __name__ == "__main__":
    test_purchase_without_token_is_rejected()
    test_register_login_and_authenticated_purchase()
    test_wrong_password_rejected()
    test_cannot_use_someone_elses_card_with_your_own_token()
    test_migrated_user_must_set_password_before_login()
