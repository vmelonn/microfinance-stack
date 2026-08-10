"""
Regression test for a real bug we hit: closing a listening socket from a
different thread than the one blocked in accept() does NOT interrupt that
thread on Linux -- it stays blocked indefinitely, which means the port
never actually frees up, which broke every test that started a second
HostSimulator on the same port right after stopping the first one.

The fix was giving the listening socket a timeout, so _accept_loop wakes up
periodically to check self._stop instead of blocking in accept() forever.
This test locks that behavior in: stop() must return quickly, AND a new
simulator must be able to bind the same port immediately afterward.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from switch.host_simulator import HostSimulator


def test_stop_returns_quickly():
    sim = HostSimulator(port=9500)
    sim.start()
    time.sleep(0.2)

    start = time.monotonic()
    sim.stop()
    elapsed = time.monotonic() - start

    # Should be bounded by the accept() timeout (0.5s), not hang indefinitely.
    assert elapsed < 1.0, f"stop() took {elapsed:.2f}s -- accept() is likely blocking again"
    print(f"stop() returned in {elapsed:.3f}s")


def test_immediate_rebind_on_same_port():
    sim1 = HostSimulator(port=9501)
    sim1.start()
    time.sleep(0.2)
    sim1.stop()

    sim2 = HostSimulator(port=9501)
    start = time.monotonic()
    sim2.start()  # would raise OSError: Address already in use, if the bug were back
    elapsed = time.monotonic() - start
    sim2.stop()

    assert elapsed < 1.0, f"Rebind took {elapsed:.2f}s -- port was not released promptly"
    print(f"Second simulator bound the same port in {elapsed:.3f}s")


def test_repeated_start_stop_cycles():
    """The exact pattern that broke originally: several quick start/stop cycles in a row."""
    for i in range(5):
        sim = HostSimulator(port=9502)
        sim.start()
        time.sleep(0.05)
        sim.stop()
    print("5 rapid start/stop cycles on the same port completed without error")


if __name__ == "__main__":
    test_stop_returns_quickly()
    test_immediate_rebind_on_same_port()
    test_repeated_start_stop_cycles()
