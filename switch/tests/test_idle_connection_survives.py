"""
Regression test for a bug introduced by the earlier shutdown fix: an
accepted connection inherits the LISTENING socket's timeout by default. We
set that listening-socket timeout to 0.5s so accept() could notice shutdown
promptly -- but without resetting it on the accepted connection, every real
connection would silently get dropped the moment it went idle for more than
0.5 seconds, which is the normal case (heartbeats are tens of seconds
apart). This was caught via a real deployment where the client was stuck in
a permanent reconnect loop even though nothing was actually wrong.

This test holds a connection open, idle, for well over 0.5 seconds, and
confirms it was NEVER dropped during that window.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from switch.client import ISO8583Client
from switch.host_simulator import HostSimulator


def test_idle_connection_survives_past_the_accept_timeout():
    sim = HostSimulator(port=9800)
    sim.start()
    time.sleep(0.2)

    client = ISO8583Client("127.0.0.1", 9800, heartbeat_interval=9999)
    client.connect()
    time.sleep(0.3)
    assert client._connected.is_set() is True

    # Sit idle for well over the old 0.5s timeout that used to kill this.
    saw_disconnect = False
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if not client._connected.is_set():
            saw_disconnect = True
        time.sleep(0.05)

    assert not saw_disconnect, "Connection was dropped during an idle period -- the timeout-inheritance bug is back"
    assert client._connected.is_set() is True

    # Prove it's still genuinely usable after sitting idle, not just "looks connected."
    received = []
    client.on_message = received.append
    stan = client.next_stan()
    client.send_message("0200", {11: stan, 3: "000000", 4: "000000001000"})

    wait_deadline = time.monotonic() + 3
    while time.monotonic() < wait_deadline and not received:
        time.sleep(0.05)

    client.close()
    sim.stop()

    assert received, "Connection looked alive but couldn't actually complete a transaction after idling"
    print("Connection survived a 2s idle period with zero disconnects, and remained genuinely usable")


if __name__ == "__main__":
    test_idle_connection_survives_past_the_accept_timeout()
