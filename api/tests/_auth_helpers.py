"""
Shared helper for api/tests/*.py -- registers a fresh, uniquely-identified
user and logs in, returning a ready-to-use auth header and their card
number. Centralized here so every test file that needs an authenticated
caller does it the same way, instead of five slightly-different copies of
the same registration boilerplate.

api/main.py uses a PERSISTENT ledger.db by design, so every call to this
helper generates fresh, unique identifiers -- safe to call many times
across many test runs without ever colliding with leftover data.
"""

import uuid


def unique_cnic() -> str:
    return str(uuid.uuid4().int)[:13]


def unique_card() -> str:
    return "9" + str(uuid.uuid4().int)[:15]


def register_and_login(client, full_name: str = "Test User", password: str = "a-real-password-123"):
    """
    Returns (auth_headers, card_number) -- auth_headers is ready to pass
    straight into client.post(..., headers=auth_headers).
    """
    cnic = unique_cnic()
    card = unique_card()

    reg = client.post("/users/register", json={
        "full_name": full_name, "cnic": cnic, "bind_card_number": card, "password": password,
    })
    assert reg.status_code == 200, f"Registration failed in test setup: {reg.text}"

    login = client.post("/auth/login", json={"cnic": cnic, "password": password})
    assert login.status_code == 200, f"Login failed in test setup: {login.text}"

    token = login.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}, card
