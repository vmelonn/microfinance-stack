"""
ClickHouse warehouse tests.

These require a real ClickHouse and skip cleanly without one:

    docker run -d --name clickhouse -p 8123:8123 \
        -e CLICKHOUSE_DB=analytics clickhouse/clickhouse-server:24-alpine

That they CAN run is the point of the move off Redshift. The old
RedshiftWarehouse was correct-shaped code no test could ever execute, so its
behaviour was asserted by reading it. These assert it by running it.

The two ReplacingMergeTree tests are the ones worth reading -- they pin
behaviour that looks fine in casual use and is wrong under retry.
"""

import os
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from analytics.sync_to_warehouse import extract_new_transactions
from analytics.warehouse import ClickHouseWarehouse

CLICKHOUSE_HOST = os.environ.get("CLICKHOUSE_HOST", "localhost")
CLICKHOUSE_PORT = int(os.environ.get("CLICKHOUSE_PORT", "8123"))


def _clickhouse_available() -> bool:
    try:
        import clickhouse_connect

        client = clickhouse_connect.get_client(host=CLICKHOUSE_HOST, port=CLICKHOUSE_PORT)
        client.command("SELECT 1")
        client.close()
        return True
    except Exception:
        return False


# Scoped to the tests that genuinely need a server, NOT applied module-wide.
#
# A module-level pytestmark would also skip the extraction tests at the
# bottom, which only touch SQLite -- and a test that silently stops running
# is worse than no test, because the suite still reports green.
requires_clickhouse = pytest.mark.skipif(
    not _clickhouse_available(),
    reason=(
        f"No ClickHouse at {CLICKHOUSE_HOST}:{CLICKHOUSE_PORT}. Start one with: "
        "docker run -d -p 8123:8123 -e CLICKHOUSE_DB=analytics "
        "clickhouse/clickhouse-server:24-alpine"
    ),
)


@pytest.fixture
def warehouse():
    """A throwaway database per test, so one test's rows cannot satisfy
    another's assertions."""
    database = f"test_{uuid.uuid4().hex[:12]}"

    import clickhouse_connect

    admin = clickhouse_connect.get_client(host=CLICKHOUSE_HOST, port=CLICKHOUSE_PORT)
    admin.command(f"CREATE DATABASE {database}")

    wh = ClickHouseWarehouse(host=CLICKHOUSE_HOST, port=CLICKHOUSE_PORT, database=database)
    wh.ensure_schema()
    yield wh

    wh.close()
    admin.command(f"DROP DATABASE {database}")
    admin.close()


def _row(rrn, cents=5000, ts=None, debit="acc_alice", credit="acc_merchant"):
    return {
        "rrn": rrn,
        "amount_cents": cents,
        "transaction_ts": ts or datetime.now(timezone.utc),
        "debit_account_id": debit,
        "credit_account_id": credit,
    }


@requires_clickhouse
def test_schema_creates_every_object(warehouse):
    tables = {r[0] for r in warehouse.execute_query("SHOW TABLES")}
    assert {"fact_transactions", "etl_watermark", "agg_daily_volume", "mv_daily_volume"} <= tables


@requires_clickhouse
def test_load_and_count(warehouse):
    assert warehouse.load_transactions([_row(f"rrn{i:09d}") for i in range(10)]) == 10
    assert warehouse.count_transactions() == 10


@requires_clickhouse
def test_reloading_the_same_rrn_does_not_double_count(warehouse):
    """
    The idempotency property that makes a retried sync safe.

    NOTE the FINAL in count_transactions(). ReplacingMergeTree deduplicates
    during background merges, at an unpredictable time -- a plain
    SELECT count() run between the insert and its merge genuinely returns 2.
    That is not a bug to work around; it is the engine's contract, and code
    that must not see duplicates has to say FINAL.
    """
    warehouse.load_transactions([_row("rrn000000001", cents=5000)])
    warehouse.load_transactions([_row("rrn000000001", cents=5000)])

    assert warehouse.count_transactions() == 1


@requires_clickhouse
def test_dedup_keeps_the_most_recent_version(warehouse):
    """ReplacingMergeTree(loaded_at) keeps the highest loaded_at, so a
    corrected re-load wins over the original."""
    ts = datetime.now(timezone.utc)
    warehouse.load_transactions([_row("rrn000000002", cents=5000, ts=ts)])
    warehouse.load_transactions([_row("rrn000000002", cents=7500, ts=ts)])

    rows = warehouse.execute_query(
        "SELECT amount_cents FROM fact_transactions FINAL WHERE rrn = 'rrn000000002'"
    )
    assert len(rows) == 1
    assert rows[0][0] == 7500


@requires_clickhouse
def test_watermark_round_trips_the_exact_source_string(warehouse):
    """
    Stored and returned verbatim, with no parsing.

    This is the fix for a real bug this project hit: comparing timestamps as
    text across engines broke incremental sync because Postgres's plain
    TIMESTAMP dropped the UTC offset SQLite's string format carried, so every
    sync re-processed the same rows forever.
    """
    assert warehouse.get_watermark("fact_transactions") is None

    source_string = "2026-08-10T14:23:45.123456+00:00"
    warehouse.set_watermark("fact_transactions", source_string, "run-abc", 42)

    assert warehouse.get_watermark("fact_transactions") == source_string


@requires_clickhouse
def test_watermark_update_returns_the_newer_value(warehouse):
    """etl_watermark is also a ReplacingMergeTree, so get_watermark uses
    FINAL. Without it this could return the older row -- and a watermark that
    goes backwards re-loads rows the materialized view would double-count."""
    warehouse.set_watermark("fact_transactions", "2026-08-01T00:00:00+00:00", "run-1", 10)
    warehouse.set_watermark("fact_transactions", "2026-08-02T00:00:00+00:00", "run-2", 20)

    assert warehouse.get_watermark("fact_transactions") == "2026-08-02T00:00:00+00:00"


@requires_clickhouse
def test_materialized_view_aggregates_at_insert_time(warehouse):
    """Daily volume is maintained incrementally, so the read is trivial
    regardless of fact table size."""
    day = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
    warehouse.load_transactions([
        _row("rrnmv000001", cents=1000, ts=day, debit="acc_alice"),
        _row("rrnmv000002", cents=2500, ts=day, debit="acc_alice"),
        _row("rrnmv000003", cents=700, ts=day, debit="acc_bob"),
    ])

    rows = warehouse.execute_query(
        "SELECT account_id, sum(txn_count), sum(total_cents) "
        "FROM agg_daily_volume WHERE day = '2026-08-10' "
        "GROUP BY account_id ORDER BY account_id"
    )
    by_account = {r[0]: (r[1], r[2]) for r in rows}

    assert by_account["acc_alice"] == (2, 3500)
    assert by_account["acc_bob"] == (1, 700)


@requires_clickhouse
def test_materialized_view_DOES_double_count_a_reloaded_row(warehouse):
    """
    Pinning the trap rather than pretending it is not there.

    A materialized view fires on INSERT, BEFORE ReplacingMergeTree
    deduplicates. So re-loading a duplicate RRN leaves fact_transactions
    correct and agg_daily_volume wrong. This test documents that, and is why
    sync_to_warehouse.py only advances its watermark after a batch is
    confirmed -- the watermark is what prevents re-loads, and therefore the
    only thing keeping the aggregates honest.

    If a future change makes the aggregate self-correcting (an
    AggregatingMergeTree over argMax, say), this test SHOULD fail and be
    replaced.
    """
    day = datetime(2026, 8, 11, 9, 0, tzinfo=timezone.utc)
    row = _row("rrndup000001", cents=1000, ts=day, debit="acc_carol")

    warehouse.load_transactions([row])
    warehouse.load_transactions([row])

    assert warehouse.count_transactions() == 1, "fact table should deduplicate"

    total = warehouse.execute_query(
        "SELECT sum(total_cents) FROM agg_daily_volume WHERE account_id = 'acc_carol'"
    )[0][0]
    assert total == 2000, (
        "expected the KNOWN double-count; if this now returns 1000 the "
        "aggregate has become self-correcting and this test is obsolete"
    )


@requires_clickhouse
def test_naive_timestamps_are_treated_as_utc_not_local(warehouse):
    """A naive timestamp interpreted in the host's timezone shifts every row
    and makes reports disagree between machines."""
    naive = datetime(2026, 8, 12, 15, 30, 0)
    warehouse.load_transactions([_row("rrntz000001", ts=naive)])

    stored = warehouse.execute_query(
        "SELECT toString(transaction_ts) FROM fact_transactions FINAL WHERE rrn = 'rrntz000001'"
    )[0][0]
    assert stored.startswith("2026-08-12 15:30:00")


@requires_clickhouse
def test_string_timestamps_from_sqlite_are_parsed(warehouse):
    """SQLite hands back ISO strings, not datetimes."""
    warehouse.load_transactions([_row("rrnstr000001", ts="2026-08-13T08:15:30+00:00")])

    stored = warehouse.execute_query(
        "SELECT toString(transaction_ts) FROM fact_transactions FINAL WHERE rrn = 'rrnstr000001'"
    )[0][0]
    assert stored.startswith("2026-08-13 08:15:30")


@requires_clickhouse
def test_empty_load_is_a_no_op(warehouse):
    assert warehouse.load_transactions([]) == 0
    assert warehouse.count_transactions() == 0


# ---------------------------------------------------------------------------
# Extraction from the operational ledger -- no ClickHouse needed
# ---------------------------------------------------------------------------

@pytest.fixture
def ledger_db(tmp_path):
    path = str(tmp_path / "ledger.db")
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE transactions (rrn TEXT PRIMARY KEY, amount_cents INTEGER, created_at TIMESTAMP);
        CREATE TABLE ledger_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT, rrn TEXT, account_id TEXT,
            entry_type TEXT, amount_cents INTEGER
        );
    """)
    base = datetime(2026, 8, 10, tzinfo=timezone.utc)
    for i in range(5):
        rrn = f"rrn{i:09d}"
        conn.execute("INSERT INTO transactions VALUES (?, ?, ?)",
                     (rrn, 1000 + i, (base + timedelta(minutes=i)).isoformat()))
        conn.execute("INSERT INTO ledger_entries (rrn, account_id, entry_type, amount_cents) "
                     "VALUES (?, 'acc_alice', 'debit', ?)", (rrn, 1000 + i))
        conn.execute("INSERT INTO ledger_entries (rrn, account_id, entry_type, amount_cents) "
                     "VALUES (?, 'acc_merchant', 'credit', ?)", (rrn, 1000 + i))
    conn.commit()
    conn.close()
    return path


def test_extract_returns_both_account_ids(ledger_db):
    """The transactions table alone has no account IDs -- only the entries
    tied to it do, which is why the query joins them twice."""
    rows = extract_new_transactions(ledger_db, None)
    assert len(rows) == 5
    assert rows[0]["debit_account_id"] == "acc_alice"
    assert rows[0]["credit_account_id"] == "acc_merchant"


def test_extract_is_ordered_so_the_watermark_advances_monotonically(ledger_db):
    rows = extract_new_transactions(ledger_db, None)
    timestamps = [r["created_at_raw"] for r in rows]
    assert timestamps == sorted(timestamps)


def test_extract_respects_the_watermark(ledger_db):
    all_rows = extract_new_transactions(ledger_db, None)
    remaining = extract_new_transactions(ledger_db, all_rows[1]["created_at_raw"])

    assert len(remaining) == 3
    assert all(r["created_at_raw"] > all_rows[1]["created_at_raw"] for r in remaining)


def test_extract_honours_the_page_limit(ledger_db):
    assert len(extract_new_transactions(ledger_db, None, limit=2)) == 2
