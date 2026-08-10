"""
Audit logging: records every ISO 8583 message in and out, with sensitive
fields masked or omitted entirely. This never affects transaction
processing -- it only observes. Built specifically to answer "what
happened to this transaction" questions later, by someone who may not have
been involved in building the system at all.
"""

import json
import threading
import time
from pathlib import Path

# These must NEVER appear in the audit log, not even masked -- PIN block,
# security control info tied to the PIN, EMV cryptogram data, and MAC fields.
NEVER_LOG = {52, 53, 55, 64, 96, 128}

# These get truncated to a safe, still-useful fragment rather than omitted.
MASK_LAST4 = {2, 34}   # PAN, PAN extended


def mask_fields(fields: dict) -> dict:
    """Returns a copy of fields safe to write to a permanent log."""
    masked = {}
    for de, value in fields.items():
        if de in NEVER_LOG:
            continue
        if de in MASK_LAST4 and isinstance(value, str) and len(value) > 4:
            masked[de] = f"...{value[-4:]}"
        else:
            masked[de] = value
    return masked


class AuditLogger:
    def __init__(self, log_path: str = None):
        self.log_path = Path(log_path) if log_path else None
        self.entries = []   # kept in memory too -- convenient for tests and quick inspection
        self._lock = threading.Lock()

    def _write(self, record: dict):
        with self._lock:
            self.entries.append(record)
            if self.log_path:
                with open(self.log_path, "a") as f:
                    f.write(json.dumps(record) + "\n")

    def log_outbound(self, mti: str, fields: dict):
        self._write({
            "timestamp": time.time(),
            "direction": "outbound",
            "mti": mti,
            "fields": mask_fields(fields),
        })

    def log_inbound(self, parsed: dict):
        self._write({
            "timestamp": time.time(),
            "direction": "inbound",
            "mti": parsed["mti"],
            "fields": mask_fields(parsed["fields"]),
        })
