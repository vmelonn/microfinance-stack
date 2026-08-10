"""
Standalone diagnostic -- deliberately has NOTHING to do with FastAPI or
uvicorn. If the same "closed after exactly N seconds" pattern shows up
here too, that proves it's not about our web server integration at all --
it's something lower-level on the machine (very possibly antivirus/EDR
software watching new local listening sockets). If it DOESN'T show up
here, that tells us the problem is specific to running inside uvicorn's
process.

Run with:
    python diagnose_connection.py

Let it run for the full 60 seconds and share everything it prints.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from switch.client import ISO8583Client
from switch.host_simulator import HostSimulator

print("=== Standalone connection diagnostic -- no FastAPI/uvicorn involved ===")

sim = HostSimulator(port=9999)
sim.start()
time.sleep(0.3)

client = ISO8583Client("127.0.0.1", 9999, heartbeat_interval=9999)  # heartbeats disabled --
client.connect()                                                     # isolates idle-timeout theories entirely

print("Watching the connection for 60 seconds, doing nothing else...")
start = time.monotonic()
last_state = None
while time.monotonic() - start < 60:
    connected = client._connected.is_set()
    if connected != last_state:
        print(f"[{time.monotonic() - start:5.1f}s] connected = {connected}")
        last_state = connected
    time.sleep(0.1)

client.close()
sim.stop()
print("=== Diagnostic complete ===")
