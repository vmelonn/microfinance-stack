"""
Tests for the auth primitives in isolation, before anything touches FastAPI.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from auth.passwords import hash_password, verify_password
from auth.tokens import create_token, decode_token, TokenError


def test_password_roundtrip():
    h = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", h) is True
    assert verify_password("wrong password", h) is False
    print("Password hash/verify round-trip OK")


def test_migrated_empty_hash_never_verifies():
    """The '' placeholder from the schema migration must never be treated as a valid password."""
    assert verify_password("anything at all", "") is False
    assert verify_password("", "") is False
    print("Empty (migrated) password hash correctly never verifies")


def test_same_password_different_hash_each_time():
    """Random salt per call -- two hashes of the same password must differ."""
    h1 = hash_password("same-password")
    h2 = hash_password("same-password")
    assert h1 != h2
    assert verify_password("same-password", h1) is True
    assert verify_password("same-password", h2) is True
    print("Same password produces different hashes (random salt), both still verify")


def test_token_roundtrip():
    token = create_token({"sub": "usr_abc123"}, secret="test-secret", expires_in_seconds=60)
    claims = decode_token(token, secret="test-secret")
    assert claims["sub"] == "usr_abc123"
    assert "exp" in claims and "iat" in claims
    print("Token create/decode round-trip OK")


def test_token_tampering_detected():
    token = create_token({"sub": "usr_abc123"}, secret="test-secret")
    header, payload, sig = token.split(".")
    tampered = f"{header}.{payload}.{sig[:-4]}XXXX"
    try:
        decode_token(tampered, secret="test-secret")
        assert False, "tampered token was accepted"
    except TokenError:
        pass
    print("Tampered token signature correctly rejected")


def test_token_wrong_secret_rejected():
    token = create_token({"sub": "usr_abc123"}, secret="secret-a")
    try:
        decode_token(token, secret="secret-b")
        assert False, "token verified with the wrong secret"
    except TokenError:
        pass
    print("Token signed with a different secret correctly rejected")


def test_token_expiry():
    token = create_token({"sub": "usr_abc123"}, secret="test-secret", expires_in_seconds=1)
    time.sleep(1.2)
    try:
        decode_token(token, secret="test-secret")
        assert False, "expired token was accepted"
    except TokenError as e:
        assert "expired" in str(e).lower()
    print("Expired token correctly rejected")


if __name__ == "__main__":
    test_password_roundtrip()
    test_migrated_empty_hash_never_verifies()
    test_same_password_different_hash_each_time()
    test_token_roundtrip()
    test_token_tampering_detected()
    test_token_wrong_secret_rejected()
    test_token_expiry()
