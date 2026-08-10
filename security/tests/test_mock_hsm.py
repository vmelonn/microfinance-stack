"""
Tests for Layer 4 (security).

1. PIN block round-trip -- encrypt then decrypt should recover the exact
   original ISO 9564 PIN block.
2. MAC tamper detection -- a modified message should fail verification.
3. Integration with Layer 1 -- the encrypted PIN block, as raw bytes, has to
   fit cleanly into DE 52 of a real built ISO 8583 message and come back out
   unchanged after a parse. This is the "sits alongside Layer 1" relationship
   from the theory made concrete: the message layer never decrypts anything,
   it just carries whatever bytes it's handed.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from security.mock_hsm import MockHSM
from security.pin_block import build_iso0_pin_block, recover_pin
from iso8583.parser import build_message, parse_message


def test_pin_block_roundtrip():
    hsm = MockHSM()
    pin = "1234"
    pan = "4532015112830366"

    ksn, encrypted = hsm.encrypt_pin_block(pin, pan)
    decrypted_block = hsm.decrypt_pin_block(ksn, encrypted)

    original_block = build_iso0_pin_block(pin, pan)
    assert decrypted_block == original_block, "Decrypted block doesn't match original"
    assert recover_pin(decrypted_block, pan) == pin, "Recovered PIN doesn't match original"
    print(f"PIN block round-trip OK -- KSN {ksn}, encrypted {encrypted.hex()}")


def test_different_ksn_gives_different_ciphertext():
    """Same PIN, same card, two transactions -- ciphertext must differ each time."""
    hsm = MockHSM()
    pin, pan = "1234", "4532015112830366"

    _, encrypted_1 = hsm.encrypt_pin_block(pin, pan)
    _, encrypted_2 = hsm.encrypt_pin_block(pin, pan)

    assert encrypted_1 != encrypted_2, "Two transactions produced identical ciphertext"
    print("Per-transaction key derivation confirmed: ciphertext differs across transactions")


def test_mac_detects_tampering():
    hsm = MockHSM()
    message = b"purchase:5000:acct123"
    mac = hsm.generate_mac(message)

    assert hsm.verify_mac(message, mac) is True

    tampered = b"purchase:9999:acct123"
    assert hsm.verify_mac(tampered, mac) is False
    print("MAC correctly detects tampering")


def test_pin_block_fits_in_de52():
    """The encrypted block, as a DE 52 field value, has to survive a full build/parse cycle."""
    hsm = MockHSM()
    pin, pan = "1234", "4532015112830366"

    ksn, encrypted = hsm.encrypt_pin_block(pin, pan)
    assert len(encrypted) == 8, "PIN block should be exactly 8 bytes, matching DE 52's FIXED length"

    # latin-1 gives a 1:1 byte<->char mapping, letting us pass raw binary
    # through the same string-based field interface every other DE uses.
    de52_value = encrypted.decode("latin-1")

    raw_message = build_message("0200", {
        3: "000000",
        4: "000000005000",
        11: "000123",
        52: de52_value,
    })

    parsed, _consumed = parse_message(raw_message)
    round_tripped_encrypted = parsed["fields"][52].encode("latin-1")

    assert round_tripped_encrypted == encrypted, "DE 52 value changed after build/parse"

    decrypted_block = hsm.decrypt_pin_block(ksn, round_tripped_encrypted)
    assert recover_pin(decrypted_block, pan) == pin

    print("DE 52 integration OK -- encrypted PIN block survived a full ISO 8583 build/parse cycle")


if __name__ == "__main__":
    test_pin_block_roundtrip()
    test_different_ksn_gives_different_ciphertext()
    test_mac_detects_tampering()
    test_pin_block_fits_in_de52()
