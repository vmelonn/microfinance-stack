"""
Regression test for a genuinely data-dependent bug: FIXED-length
non-numeric fields (DE 52 PIN block, DE 64/96/128 MAC fields) were being
.rstrip()'d on decode -- correct for space-padded TEXT fields like DE 43's
merchant name, wrong for binary content. Random ciphertext that happened
to end in a byte decoding to a whitespace codepoint (plain space, tab,
newline, or various latin-1 whitespace like NBSP) would have its trailing
byte(s) silently stripped, corrupting the value.

Found via a genuinely flaky test failure -- security/tests/test_mock_hsm.py
occasionally failed because the random encrypted PIN block happened to end
in such a byte. This test reproduces it DETERMINISTICALLY, using a value
deliberately constructed to end in 0x20 (space), rather than relying on
random chance to occasionally trigger it -- exactly the property that let
this bug hide for as long as it did.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from iso8583.parser import build_message, parse_message


def test_binary_field_surviving_trailing_whitespace_byte():
    """DE 52 (PIN block) is FIXED, 8 bytes, binary. Deliberately ends in 0x20 (space)."""
    binary_value = bytes([0x9A, 0x3F, 0x01, 0x77, 0xE2, 0x00, 0xFF, 0x20])  # last byte = space
    de52_value = binary_value.decode("latin-1")

    raw = build_message("0200", {11: "000001", 52: de52_value})
    parsed, _ = parse_message(raw)

    round_tripped = parsed["fields"][52].encode("latin-1")
    assert round_tripped == binary_value, (
        f"DE 52 was corrupted -- expected {binary_value!r}, got {round_tripped!r}. "
        "This is the rstrip()-on-binary-data bug if they differ."
    )
    print("DE 52 with a trailing 0x20 byte survived build/parse intact")


def test_binary_field_surviving_multiple_trailing_whitespace_bytes():
    """Worse case: value ends in SEVERAL bytes that each independently look like whitespace."""
    binary_value = bytes([0x11, 0x22, 0x33, 0x44, 0x0A, 0x09, 0x20, 0x0D])  # last 4 bytes: \n \t space \r
    de64_value = binary_value.decode("latin-1")

    raw = build_message("0800", {11: "000002", 64: de64_value})
    parsed, _ = parse_message(raw)

    round_tripped = parsed["fields"][64].encode("latin-1")
    assert round_tripped == binary_value, (
        f"DE 64 (MAC) was corrupted -- expected {binary_value!r}, got {round_tripped!r}"
    )
    print("DE 64 with multiple trailing whitespace-like bytes survived build/parse intact")


def test_text_field_padding_still_correctly_trimmed():
    """The fix must not break the thing rstrip() was ALWAYS correctly doing: text field padding."""
    raw = build_message("0200", {11: "000003", 43: "A Merchant"})  # DE 43 is FIXED 40, text, space-padded
    parsed, _ = parse_message(raw)
    assert parsed["fields"][43] == "A Merchant", f"Expected padding trimmed, got {parsed['fields'][43]!r}"
    print("Text field (DE 43) padding is still correctly trimmed -- fix didn't break the real case")


if __name__ == "__main__":
    test_binary_field_surviving_trailing_whitespace_byte()
    test_binary_field_surviving_multiple_trailing_whitespace_bytes()
    test_text_field_padding_still_correctly_trimmed()
