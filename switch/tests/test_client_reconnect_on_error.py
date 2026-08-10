"""
Regression test for a real bug found while debugging a live deployment:
if _receive_loop() raised anything other than ConnectionError/OSError, the
exception escaped _connection_loop entirely, skipping self._connected.clear().
That left the client LOOKING connected (so sending, including heartbeats,
kept "succeeding") while nothing could ever be received again -- every real
transaction would silently time out forever, with no obvious symptom other
than a climbing STAN counter and a health check that might report
connected=True right up until the moment you tried to actually use it.

This test forces exactly that failure (a parse_message that raises a
generic exception once) and confirms the client notices, marks itself
disconnected, and successfully reconnects and resumes normal operation.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import switch.client as client_module
from switch.client import ISO8583Client
from switch.host_simulator import HostSimulator


def test_client_recovers_from_unexpected_receive_error():
    sim = HostSimulator(port=9700)
    sim.start()
    time.sleep(0.2)

    client = ISO8583Client("127.0.0.1", 9700, heartbeat_interval=9999)
    client.connect()
    time.sleep(0.3)
    assert client._connected.is_set() is True

    # Inject exactly one failure into parse_message, simulating whatever
    # unexpected condition triggered this bug in the field.
    real_parse_message = client_module.parse_message
    failure_injected = {"done": False}

    def flaky_parse_message(raw, offset=0):
        if not failure_injected["done"]:
            failure_injected["done"] = True
            raise ValueError("simulated unexpected parsing failure")
        return real_parse_message(raw, offset)

    client_module.parse_message = flaky_parse_message

    try:
        # Trigger an inbound message so the injected failure actually fires --
        # a sign-on round trip is enough.
        client.send_message("0800", {11: client.next_stan(), 70: "001"})

        # The client should notice, mark itself disconnected, and reconnect --
        # give it a few seconds, well within its reconnect_delay + backoff.
        deadline = time.monotonic() + 6
        saw_disconnected = False
        while time.monotonic() < deadline:
            if not client._connected.is_set():
                saw_disconnected = True
            if saw_disconnected and client._connected.is_set():
                break
            time.sleep(0.1)

        assert saw_disconnected, "Client never marked itself disconnected after the injected failure"
        assert client._connected.is_set() is True, "Client did not reconnect after the failure"

    finally:
        client_module.parse_message = real_parse_message

    # Prove it's genuinely usable again, not just "looks connected."
    received = []
    client.on_message = received.append
    stan = client.next_stan()
    client.send_message("0200", {11: stan, 3: "000000", 4: "000000001000"})

    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not received:
        time.sleep(0.05)

    client.close()
    sim.stop()

    assert received, "Client reconnected but still couldn't complete a real transaction"
    print("Client correctly recovered from an unexpected receive-loop error and resumed normal operation")


if __name__ == "__main__":
    test_client_recovers_from_unexpected_receive_error()
