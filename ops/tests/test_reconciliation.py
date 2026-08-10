"""
Tests for the reconciliation half of Layer 8. Each of the four possible
outcomes gets its own case: a clean match, something only in our ledger,
something only in the switch's settlement file, and an amount that
disagrees between the two.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from ledger.db import get_connection, init_db
from ledger.service import record_purchase
from ops.reconciliation import reconcile


def _temp_db_path():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    return path


def _fresh_db(*account_ids):
    """Same seeding helper as ledger/tests/test_ledger.py -- see that file
    for why this is necessary now that ledger_entries.account_id is a real
    foreign key into accounts, not an arbitrary string."""
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
    return conn


def test_clean_reconciliation():
    conn = _fresh_db("card:0001", "merchant:demo")
    record_purchase(conn, rrn="000001", debit_account="card:0001", credit_account="merchant:demo", amount_cents=5000)

    settlement = [{"rrn": "000001", "amount_cents": 5000}]
    result = reconcile(conn, settlement)

    assert result.is_clean is True
    assert result.matched == ["000001"]
    print("Clean reconciliation:", result.summary())


def test_only_in_ledger_flagged():
    """We think a transaction happened, but the switch's settlement file has no record of it."""
    conn = _fresh_db("card:0002", "merchant:demo")
    record_purchase(conn, rrn="000002", debit_account="card:0002", credit_account="merchant:demo", amount_cents=3000)

    settlement = []  # switch never settled it
    result = reconcile(conn, settlement)

    assert result.is_clean is False
    assert result.only_in_ledger == ["000002"]
    print("Only-in-ledger correctly flagged:", result.summary())


def test_only_in_settlement_flagged():
    """The switch says a transaction happened, but we have no record of it at all."""
    conn = _fresh_db()  # ledger has nothing at all -- no accounts needed either
    # ledger has nothing at all

    settlement = [{"rrn": "000003", "amount_cents": 7500}]
    result = reconcile(conn, settlement)

    assert result.is_clean is False
    assert result.only_in_settlement == ["000003"]
    print("Only-in-settlement correctly flagged:", result.summary())


def test_amount_mismatch_flagged():
    conn = _fresh_db("card:0004", "merchant:demo")
    record_purchase(conn, rrn="000004", debit_account="card:0004", credit_account="merchant:demo", amount_cents=5000)

    settlement = [{"rrn": "000004", "amount_cents": 5500}]  # switch settled a DIFFERENT amount
    result = reconcile(conn, settlement)

    assert result.is_clean is False
    assert len(result.amount_mismatches) == 1
    assert result.amount_mismatches[0]["ledger_amount_cents"] == 5000
    assert result.amount_mismatches[0]["settlement_amount_cents"] == 5500
    print("Amount mismatch correctly flagged:", result.summary())


def test_mixed_realistic_batch():
    """A batch with a bit of everything, like a real day's reconciliation would have."""
    conn = _fresh_db("card:1", "card:2", "card:3", "merchant:demo")
    record_purchase(conn, rrn="A", debit_account="card:1", credit_account="merchant:demo", amount_cents=1000)
    record_purchase(conn, rrn="B", debit_account="card:2", credit_account="merchant:demo", amount_cents=2000)
    record_purchase(conn, rrn="C", debit_account="card:3", credit_account="merchant:demo", amount_cents=3000)

    settlement = [
        {"rrn": "A", "amount_cents": 1000},   # matches
        {"rrn": "B", "amount_cents": 9999},   # mismatch
        {"rrn": "D", "amount_cents": 4000},   # only in settlement
        # C is missing entirely -- only in ledger
    ]

    result = reconcile(conn, settlement)
    assert result.matched == ["A"]
    assert result.only_in_ledger == ["C"]
    assert result.only_in_settlement == ["D"]
    assert len(result.amount_mismatches) == 1 and result.amount_mismatches[0]["rrn"] == "B"
    print("Mixed batch reconciliation:", result.summary())


if __name__ == "__main__":
    test_clean_reconciliation()
    test_only_in_ledger_flagged()
    test_only_in_settlement_flagged()
    test_amount_mismatch_flagged()
    test_mixed_realistic_batch()
