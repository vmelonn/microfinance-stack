"""
End-to-end check for Layer 3 (correlation):

1. Normal case -- send_and_wait() blocks and returns the matched response,
   proving STAN-based correlation actually works over the real socket
   connection from Layer 2.

2. Timeout case -- a request the simulator is told to stay silent on should
   cause send_and_wait() to raise TransactionTimeout, AND a 0400 reversal
   should show up in the simulator's received log -- proving the reversal
   was sent automatically, without the caller doing anything extra.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from switch.client import ISO8583Client
from switch.host_simulator import HostSimulator
from correlation.tracker import CorrelationManager, TransactionTimeout


def test_normal_response():
    sim = HostSimulator(port=9002)
    sim.start()
    time.sleep(0.2)

    client = ISO8583Client("127.0.0.1", 9002, heartbeat_interval=9999)
    client.connect()
    time.sleep(0.3)

    correlator = CorrelationManager(client, timeout_seconds=3)

    response = correlator.send_and_wait("0200", {
        3: "000000",
        4: "000000005000",
        37: "000123456789",
    })

    client.close()
    sim.stop()

    assert response["mti"] == "0210"
    assert response["fields"][39] == "00"
    print("Layer 3 normal response OK -- matched:", response)


def test_timeout_triggers_reversal():
    sim = HostSimulator(port=9003)
    sim.start()
    time.sleep(0.2)

    client = ISO8583Client("127.0.0.1", 9003, heartbeat_interval=9999)
    client.connect()
    time.sleep(0.3)

    correlator = CorrelationManager(client, timeout_seconds=1)

    raised = False
    try:
        correlator.send_and_wait("0200", {
            3: "000000",
            4: "000000005000",
            48: "SIMULATE_TIMEOUT",   # tells the simulator to stay silent
        })
    except TransactionTimeout as e:
        raised = True
        print("Timeout correctly raised:", e)

    time.sleep(0.3)  # give the reversal a moment to arrive at the simulator

    client.close()
    sim.stop()

    assert raised, "Expected TransactionTimeout to be raised"
    reversal_messages = [m for m in sim.received if m["mti"] == "0400"]
    assert reversal_messages, "Expected a 0400 reversal to have been sent"
    print("Layer 3 timeout -> reversal OK -- simulator received:", reversal_messages[0])


if __name__ == "__main__":
    test_normal_response()
    test_timeout_triggers_reversal()
