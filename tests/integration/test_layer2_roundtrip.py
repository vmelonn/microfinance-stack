"""
End-to-end check for Layer 2: start the host simulator, connect the client,
send a purchase, and confirm an approved response comes back over the wire.

Layer 3 (correlation) doesn't exist yet, so this test matches the response
manually rather than relying on any STAN-based lookup -- that's the whole
point: Layer 2 alone should already be enough to prove bytes go out and
parsed messages come back correctly.
"""

import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from switch.client import ISO8583Client
from switch.host_simulator import HostSimulator


def test_roundtrip():
    sim = HostSimulator(port=9001)
    sim.start()
    time.sleep(0.2)  # give the simulator a moment to start listening

    received = []
    lock = threading.Lock()

    def on_message(parsed):
        with lock:
            received.append(parsed)

    client = ISO8583Client("127.0.0.1", 9001, heartbeat_interval=9999, on_message=on_message)
    client.connect()
    time.sleep(0.3)  # give the client a moment to connect and sign on

    stan = client.next_stan()
    client.send_message("0200", {
        11: stan,
        3: "000000",
        4: "000000005000",
        37: "000123456789",
    })

    deadline = time.time() + 3
    purchase_response = None
    while time.time() < deadline:
        with lock:
            matches = [m for m in received if m["mti"] == "0210"]
        if matches:
            purchase_response = matches[0]
            break
        time.sleep(0.05)

    client.close()
    sim.stop()

    assert purchase_response is not None, "No 0210 response received from host simulator"
    assert purchase_response["fields"][39] == "00", "Expected an approved response"
    print("Layer 2 round trip OK -- received:", purchase_response)


if __name__ == "__main__":
    test_roundtrip()
