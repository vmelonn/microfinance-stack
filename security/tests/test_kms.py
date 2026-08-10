"""
Tests for security/kms.py, and the specific real problem it exists to
solve: MockHSM's base_key used to be lost on every process restart.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from security.kms import LocalKeyManagementService
from security.mock_hsm import MockHSM
from security.pin_block import recover_pin


def test_generate_and_decrypt_data_key_roundtrip():
    kms = LocalKeyManagementService()
    plaintext, encrypted = kms.generate_data_key("test-key")
    assert len(plaintext) == 32  # AES-256
    recovered = kms.decrypt_data_key(encrypted, "test-key")
    assert recovered == plaintext
    print("Data key round-trip OK")


def test_each_data_key_is_unique():
    kms = LocalKeyManagementService()
    _, encrypted_a = kms.generate_data_key("test-key")
    _, encrypted_b = kms.generate_data_key("test-key")
    assert encrypted_a != encrypted_b, "Two calls produced identical encrypted keys -- nonce reuse bug"
    print("Each generate_data_key() call produces a genuinely unique result")


def test_wrong_key_id_is_rejected():
    """The key_id is bound in as authenticated associated data -- a wrapped key
    can't be silently reused to answer for a different key_id."""
    kms = LocalKeyManagementService()
    _, encrypted = kms.generate_data_key("key-a")
    try:
        kms.decrypt_data_key(encrypted, "key-b")
        assert False, "decrypted successfully under the wrong key_id"
    except Exception:
        pass
    print("Wrong key_id correctly rejected")


def test_tampered_ciphertext_is_rejected():
    kms = LocalKeyManagementService()
    _, encrypted = kms.generate_data_key("test-key")
    tampered = encrypted[:-1] + bytes([encrypted[-1] ^ 0xFF])
    try:
        kms.decrypt_data_key(tampered, "test-key")
        assert False, "decrypted successfully after tampering"
    except Exception:
        pass
    print("Tampered ciphertext correctly rejected (AES-GCM authentication)")


def test_hsm_key_survives_a_simulated_restart():
    """
    The actual problem this feature exists to solve: without persistence,
    a fresh MockHSM() gets a brand-new random base_key every time, so
    anything encrypted before a restart becomes permanently undecryptable.
    This proves that no longer happens when a KMS + persisted path are given.
    """
    key_path = tempfile.mktemp(suffix=".enc")
    # Same master key across both "runs" -- imagine this loaded from a real
    # KMS/secrets manager, not regenerated each time.
    kms = LocalKeyManagementService(master_key=b"a-stable-master-key-32-bytes!!!!")

    try:
        hsm_run1 = MockHSM(kms=kms, persisted_key_path=key_path)
        pin, pan = "1234", "4532015112830366"
        ksn, encrypted_block = hsm_run1.encrypt_pin_block(pin, pan)

        # Simulate a full process restart: a brand new MockHSM instance,
        # nothing shared with hsm_run1 except the file on disk.
        hsm_run2 = MockHSM(kms=kms, persisted_key_path=key_path)

        assert hsm_run1.base_key == hsm_run2.base_key, "Restart produced a different base_key -- persistence failed"

        decrypted_block = hsm_run2.decrypt_pin_block(ksn, encrypted_block)
        recovered_pin = recover_pin(decrypted_block, pan)
        assert recovered_pin == pin, "Could not decrypt a PIN block encrypted before the simulated restart"

        print("PIN block encrypted before a simulated restart was correctly decrypted after it")
    finally:
        if os.path.exists(key_path):
            os.remove(key_path)


def test_hsm_without_kms_still_works_unchanged():
    """Backward compatibility: no kms/persisted_key_path given -- today's ephemeral behavior, untouched."""
    hsm = MockHSM()
    ksn, encrypted = hsm.encrypt_pin_block("1234", "4532015112830366")
    decrypted = hsm.decrypt_pin_block(ksn, encrypted)
    assert recover_pin(decrypted, "4532015112830366") == "1234"
    print("MockHSM with no KMS configured still works exactly as before")


if __name__ == "__main__":
    test_generate_and_decrypt_data_key_roundtrip()
    test_each_data_key_is_unique()
    test_wrong_key_id_is_rejected()
    test_tampered_ciphertext_is_rejected()
    test_hsm_key_survives_a_simulated_restart()
    test_hsm_without_kms_still_works_unchanged()
