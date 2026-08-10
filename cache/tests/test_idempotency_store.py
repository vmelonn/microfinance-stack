"""
Tests for the idempotency store. Every test runs against BOTH the
in-memory and Redis implementations (via parametrize-by-hand, since this
project doesn't use pytest), proving they share identical behavior --
that's the whole point of the swappable interface.
"""

import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from cache.idempotency_store import InMemoryIdempotencyStore, RedisIdempotencyStore


def _make_stores():
    import redis
    r = redis.Redis(host="127.0.0.1", port=6379, db=15)  # db 15 -- keep test data out of the way
    r.flushdb()
    return {
        "in-memory": InMemoryIdempotencyStore(),
        "redis": RedisIdempotencyStore(r),
    }


def test_first_claim_is_new():
    for name, store in _make_stores().items():
        outcome = store.claim("key-1", "hash-a")
        assert outcome.status == "new", f"[{name}] expected 'new', got {outcome.status}"
        print(f"[{name}] first claim on a fresh key: new")


def test_duplicate_after_response_stored():
    for name, store in _make_stores().items():
        store.claim("key-2", "hash-a")
        store.store_response("key-2", {"status": "approved"})

        outcome = store.claim("key-2", "hash-a")
        assert outcome.status == "duplicate"
        assert outcome.cached_response == {"status": "approved"}
        print(f"[{name}] duplicate correctly returns the cached response")


def test_mismatched_hash_is_rejected():
    for name, store in _make_stores().items():
        store.claim("key-3", "hash-a")
        outcome = store.claim("key-3", "hash-b")   # same key, DIFFERENT body
        assert outcome.status == "mismatch"
        print(f"[{name}] mismatched request body on a reused key correctly rejected")


def test_claimed_but_not_yet_finished_is_in_progress():
    for name, store in _make_stores().items():
        store.claim("key-4", "hash-a")
        # No store_response() call yet -- still "in flight"
        outcome = store.claim("key-4", "hash-a")
        assert outcome.status == "in_progress"
        print(f"[{name}] a claim with no stored response yet correctly reports in_progress")


def test_concurrent_claims_only_one_wins():
    """The actual race this whole feature exists to close."""
    for name, store in _make_stores().items():
        results = []
        results_lock = threading.Lock()

        def attempt():
            outcome = store.claim("key-race", "hash-a")
            with results_lock:
                results.append(outcome.status)

        threads = [threading.Thread(target=attempt) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        new_count = results.count("new")
        assert new_count == 1, f"[{name}] expected exactly 1 'new' among 20 concurrent claims, got {new_count} -- {results}"
        print(f"[{name}] 20 concurrent claims on the same key -> exactly 1 'new', rest correctly blocked")


if __name__ == "__main__":
    test_first_claim_is_new()
    test_duplicate_after_response_stored()
    test_mismatched_hash_is_rejected()
    test_claimed_but_not_yet_finished_is_in_progress()
    test_concurrent_claims_only_one_wins()
