"""
Tests for the audit logging half of Layer 8.

1. mask_fields() in isolation -- confirms the exact masking rules.
2. A real transaction over a real socket -- confirms the audit logger
   genuinely captures what went over the wire, with the PIN block absent
   and the PAN truncated, not just in a unit test of the masking function.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from ops.audit_log import AuditLogger, mask_fields
from switch.client import ISO8583Client
from switch.host_simulator import HostSimulator


def test_pan_is_masked_to_last_four():
    masked = mask_fields({2: "4532015112830366", 3: "000000"})
    assert masked[2] == "...0366"
    assert masked[3] == "000000"
    print("PAN masked correctly:", masked)


def test_pin_block_and_security_fields_never_appear():
    masked = mask_fields({2: "4532015112830366", 52: "some-encrypted-bytes", 53: "0000000000000001", 64: "mac-bytes"})
    assert 52 not in masked
    assert 53 not in masked
    assert 64 not in masked
    assert 2 in masked
    print("Sensitive fields correctly absent from the masked output:", masked)


def test_audit_logger_captures_a_real_transaction():
    sim = HostSimulator(port=9600)
    sim.start()
    time.sleep(0.2)

    audit_logger = AuditLogger()
    client = ISO8583Client("127.0.0.1", 9600, heartbeat_interval=9999, audit_logger=audit_logger)
    client.connect()
    time.sleep(0.3)

    client.send_message("0200", {
        11: client.next_stan(),
        2: "4532015112830366",
        4: "000000005000",
        52: "fake-encrypted-pin-block",
    })
    time.sleep(0.3)  # let the response come back and get logged too

    client.close()
    sim.stop()

    outbound = [e for e in audit_logger.entries if e["direction"] == "outbound" and e["mti"] == "0200"]
    assert len(outbound) == 1
    logged_fields = outbound[0]["fields"]

    assert logged_fields[2] == "...0366", "PAN should be masked in the real logged entry"
    assert 52 not in logged_fields, "PIN block should never appear in the audit log"

    inbound = [e for e in audit_logger.entries if e["direction"] == "inbound" and e["mti"] == "0210"]
    assert len(inbound) == 1

    print(f"Audit log captured {len(audit_logger.entries)} total entries across the real connection")
    print("Logged outbound purchase (masked):", outbound[0])


if __name__ == "__main__":
    test_pan_is_masked_to_last_four()
    test_pin_block_and_security_fields_never_appear()
    test_audit_logger_captures_a_real_transaction()
