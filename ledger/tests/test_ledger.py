"""
Tests for Layer 6.

1. A normal purchase creates a balanced debit/credit pair.
2. Recording the same RRN twice, sequentially, is a safe no-op the second time.
3. The important one: recording the same RRN from several threads AT ONCE
   still results in exactly one transaction being recorded -- proving the
   protection is a genuine database-level guarantee, not just "check first,
   then insert" application logic that could itself race.
"""

import os
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from ledger.db import get_connection, init_db
from ledger.service import record_purchase, get_balance, is_balanced, find_by_rrn


def _temp_db_path():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)  # let init_db create it fresh
    return path


def _fresh_db(*account_ids):
    """
    Sets up a fresh, properly-schema'd temp database (via init_db, not the
    old implicit auto-creation), and seeds a minimal user + account row for
    each account_id the test is about to use -- the schema now enforces a
    real foreign key from ledger_entries.account_id to accounts.account_id,
    so a bare string like "card:0366" is no longer valid on its own.
    """
    path = _temp_db_path()
    init_db(path)
    conn = get_connection(path)
    for i, account_id in enumerate(account_ids):
        user_id = f"seed-user-{i}-{account_id}"
        conn.execute(
            "INSERT INTO users (user_id, full_name, cnic, password_hash) VALUES (?, ?, ?, ?)",
            (user_id, "Seed User", f"{i:013d}", "unused-in-these-tests"),
        )
        conn.execute(
            "INSERT INTO accounts (account_id, user_id, type) VALUES (?, ?, 'checking')",
            (account_id, user_id),
        )
    conn.commit()
    return path, conn


def test_record_and_balance():
    db_path, conn = _fresh_db("card:0366", "merchant:demo")

    result = record_purchase(conn, rrn="000123456789", debit_account="card:0366",
                              credit_account="merchant:demo", amount_cents=5000)
    assert result["status"] == "recorded"

    assert get_balance(conn, "card:0366") == -5000       # debited
    assert get_balance(conn, "merchant:demo") == 5000     # credited
    assert is_balanced(conn) is True
    print("Record + balance OK:", result)


def test_duplicate_rrn_sequential_is_safe_noop():
    db_path, conn = _fresh_db("card:1111", "merchant:demo")

    first = record_purchase(conn, rrn="000999", debit_account="card:1111",
                             credit_account="merchant:demo", amount_cents=2500)
    second = record_purchase(conn, rrn="000999", debit_account="card:1111",
                              credit_account="merchant:demo", amount_cents=2500)

    assert first["status"] == "recorded"
    assert second["status"] == "already_recorded"
    # Balance should reflect ONE transaction, not two.
    assert get_balance(conn, "card:1111") == -2500
    print("Sequential duplicate RRN correctly ignored on the second call")


def test_concurrent_duplicate_rrn_only_one_wins():
    db_path, _seed_conn = _fresh_db("card:2222", "merchant:demo")
    _seed_conn.close()

    results = []
    results_lock = threading.Lock()

    def attempt():
        conn = get_connection(db_path)   # each thread gets its own connection
        result = record_purchase(conn, rrn="000777", debit_account="card:2222",
                                  credit_account="merchant:demo", amount_cents=10000)
        with results_lock:
            results.append(result["status"])
        conn.close()

    threads = [threading.Thread(target=attempt) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    recorded_count = results.count("recorded")
    already_count = results.count("already_recorded")

    assert recorded_count == 1, f"Expected exactly 1 'recorded', got {recorded_count} -- {results}"
    assert already_count == 9, f"Expected 9 'already_recorded', got {already_count}"

    conn = get_connection(db_path)
    assert get_balance(conn, "card:2222") == -10000, "Balance shows only ONE transaction was actually applied"
    assert is_balanced(conn) is True
    print(f"10 concurrent attempts at the same RRN -> exactly 1 recorded, 9 correctly rejected")


if __name__ == "__main__":
    test_record_and_balance()
    test_duplicate_rrn_sequential_is_safe_noop()
    test_concurrent_duplicate_rrn_only_one_wins()
