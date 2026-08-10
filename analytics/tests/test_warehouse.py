"""
Tests for analytics/warehouse.py, against the real local Postgres.

test_timestamp_roundtrip_preserves_timezone_for_comparison is a dedicated
regression test for a real bug found while building this: the warehouse's
transaction_ts column was originally a plain TIMESTAMP (no timezone),
which meant a value read back from Postgres lost its UTC offset -- so
comparing it as a string against SQLite's own timestamp format (which
DOES include the offset) never correctly recognized already-synced rows,
silently defeating incremental sync. Fixed with TIMESTAMPTZ.
"""

import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from analytics.warehouse import LocalWarehouse

TEST_DSN = "postgresql://practice_user:practice_pass123@127.0.0.1:5432/microfinance_warehouse"


def _fresh_warehouse():
    wh = LocalWarehouse(TEST_DSN)
    with wh._connect() as conn:
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS fact_transactions")
        conn.commit()
    wh.ensure_schema()
    return wh


def test_empty_warehouse_has_no_latest_timestamp():
    wh = _fresh_warehouse()
    assert wh.get_latest_loaded_timestamp() is None
    print("Empty warehouse correctly reports no latest timestamp")


def test_load_and_query():
    wh = _fresh_warehouse()
    rows = [{
        "rrn": "test-rrn-1", "amount_cents": 5000,
        "transaction_ts": datetime(2026, 8, 10, 12, 0, 0, tzinfo=timezone.utc),
        "debit_account_id": "acc_a", "credit_account_id": "acc_b",
    }]
    loaded = wh.load_transactions(rows)
    assert loaded == 1
    result = wh.execute_query("SELECT rrn, amount_cents FROM fact_transactions WHERE rrn = %s", ("test-rrn-1",))
    assert result == [("test-rrn-1", 5000)]
    print("Load and query round-trip OK")


def test_duplicate_rrn_does_not_double_load():
    wh = _fresh_warehouse()
    row = {
        "rrn": "test-rrn-dup", "amount_cents": 5000,
        "transaction_ts": datetime(2026, 8, 10, 12, 0, 0, tzinfo=timezone.utc),
        "debit_account_id": "acc_a", "credit_account_id": "acc_b",
    }
    wh.load_transactions([row])
    wh.load_transactions([row])  # same RRN again
    result = wh.execute_query("SELECT count(*) FROM fact_transactions WHERE rrn = %s", ("test-rrn-dup",))
    assert result[0][0] == 1, "Same RRN loaded twice should not create two rows"
    print("Duplicate RRN correctly did not double-load")


def test_timestamp_roundtrip_preserves_timezone_for_comparison():
    """The actual regression test for the real bug this session found."""
    wh = _fresh_warehouse()
    original_ts = datetime(2026, 8, 10, 5, 28, 48, 932250, tzinfo=timezone.utc)
    wh.load_transactions([{
        "rrn": "tz-test-rrn", "amount_cents": 1000, "transaction_ts": original_ts,
        "debit_account_id": "acc_a", "credit_account_id": "acc_b",
    }])

    roundtripped = wh.get_latest_loaded_timestamp()
    assert roundtripped.tzinfo is not None, "Timestamp lost its timezone info on round-trip -- the bug is back"

    # The actual thing that matters: does this correctly compare against
    # the exact string format SQLite/Python's datetime.isoformat() produces?
    sqlite_style_string = original_ts.isoformat()  # what ledger/service.py actually writes
    roundtripped_string = roundtripped.isoformat()
    assert roundtripped_string == sqlite_style_string, (
        f"Round-tripped timestamp format doesn't match the source format -- "
        f"got {roundtripped_string!r}, expected {sqlite_style_string!r}. "
        f"This is exactly the bug that silently broke incremental sync."
    )
    print("Timestamp round-trips with timezone intact, matching SQLite's own format exactly")


if __name__ == "__main__":
    test_empty_warehouse_has_no_latest_timestamp()
    test_load_and_query()
    test_duplicate_rrn_does_not_double_load()
    test_timestamp_roundtrip_preserves_timezone_for_comparison()
