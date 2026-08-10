"""
Demo: three transactions sent nearly simultaneously, on three different
application threads -- each simulating a different customer tapping "pay"
at almost the same moment. The FIRST one sent is deliberately made the
SLOWEST to respond, forcing the responses to arrive back in a different
order than they were sent, so we can watch CorrelationManager still match
each one to the correct caller.
"""

import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from switch.client import ISO8583Client
from switch.host_simulator import HostSimulator
from correlation.tracker import CorrelationManager


def run_demo():
    sim = HostSimulator(port=9010)
    sim.start()
    time.sleep(0.2)

    client = ISO8583Client("127.0.0.1", 9010, heartbeat_interval=9999)
    client.connect()
    time.sleep(0.3)

    correlator = CorrelationManager(client, timeout_seconds=5)

    # (label, artificial delay before the simulator responds, amount)
    transactions = [
        ("Customer A", 1.2, "000000010000"),  # sent first, but slowest to respond
        ("Customer B", 0.4, "000000002500"),
        ("Customer C", 0.0, "000000007500"),  # sent last, but fastest to respond
    ]

    results = {}
    results_lock = threading.Lock()

    def send_one(label, delay, amount):
        sent_at = time.monotonic()
        response = correlator.send_and_wait("0200", {
            3: "000000",
            4: amount,
            48: f"DELAY:{delay}",
        })
        elapsed = time.monotonic() - sent_at
        with results_lock:
            results[label] = (response["fields"][11], elapsed)
        print(f"[{time.monotonic():.3f}] {label} matched -- "
              f"STAN {response['fields'][11]}, "
              f"waited {elapsed:.2f}s, response code {response['fields'][39]}")

    threads = []
    print("Sending all three transactions at once:\n")
    for label, delay, amount in transactions:
        t = threading.Thread(target=send_one, args=(label, delay, amount))
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    client.close()
    sim.stop()

    print("\nEach customer received exactly their own response, matched by STAN,")
    print("regardless of the order the underlying network happened to deliver them in.")


if __name__ == "__main__":
    run_demo()
