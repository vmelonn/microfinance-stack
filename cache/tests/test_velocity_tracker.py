"""
Tests for the velocity tracker. Same pattern as the idempotency store
tests: every check runs against both implementations, proving they share
identical sliding-window behavior.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from cache.velocity_tracker import InMemoryVelocityTracker, RedisVelocityTracker


def _make_trackers():
    import redis
    r = redis.Redis(host="127.0.0.1", port=6379, db=15)
    r.flushdb()
    return {
        "in-memory": InMemoryVelocityTracker(),
        "redis": RedisVelocityTracker(r),
    }


def test_count_increases_with_each_attempt():
    for name, tracker in _make_trackers().items():
        counts = [tracker.record_and_count_recent("card-1", window_seconds=60) for _ in range(5)]
        assert counts == [1, 2, 3, 4, 5], f"[{name}] expected [1,2,3,4,5], got {counts}"
        print(f"[{name}] count increases correctly with each attempt: {counts}")


def test_different_cards_tracked_independently():
    for name, tracker in _make_trackers().items():
        tracker.record_and_count_recent("card-a", window_seconds=60)
        tracker.record_and_count_recent("card-a", window_seconds=60)
        count_b = tracker.record_and_count_recent("card-b", window_seconds=60)
        assert count_b == 1, f"[{name}] card-b's count should be unaffected by card-a's attempts, got {count_b}"
        print(f"[{name}] cards tracked independently, no cross-contamination")


def test_old_attempts_fall_out_of_the_window():
    for name, tracker in _make_trackers().items():
        tracker.record_and_count_recent("card-expiring", window_seconds=0.5)
        tracker.record_and_count_recent("card-expiring", window_seconds=0.5)
        time.sleep(0.7)  # let both attempts age out of the 0.5s window
        count = tracker.record_and_count_recent("card-expiring", window_seconds=0.5)
        assert count == 1, f"[{name}] old attempts should have aged out, only this new one should count, got {count}"
        print(f"[{name}] attempts outside the window correctly excluded from the count")


if __name__ == "__main__":
    test_count_increases_with_each_attempt()
    test_different_cards_tracked_independently()
    test_old_attempts_fall_out_of_the_window()
