"""
Incremental sync from the operational ledger (SQLite, tuned for
per-transaction reads and writes) into the ClickHouse warehouse (tuned for
scanning everything at once).

Same "runs on its own schedule, not per-transaction" shape as
ops/run_reconciliation.py -- this is what a Kubernetes CronJob invokes,
hourly.

INCREMENTAL, NOT A FULL RELOAD. Asks the warehouse for its watermark and
pulls only rows newer than that. A full reload is fine at this project's
scale and a genuinely bad idea at real warehouse scale, so it is built the
way it would actually have to work.

WATERMARK DISCIPLINE, and why it is a correctness requirement rather than
tidiness. The watermark advances only after a batch is confirmed loaded. On
failure it stays put and the next run re-reads the same rows. Re-reading is
safe for fact_transactions -- ReplacingMergeTree collapses duplicates -- but
NOT for the mv_daily_volume materialized view, which fires on insert, before
deduplication. So a watermark that advanced past an unconfirmed batch would
leave the aggregates permanently wrong while the fact table looked fine.

Usage:
    python3 analytics/sync_to_warehouse.py <ledger_db_path>

Configuration, same environment-variable pattern as REDIS_URL and
HSM_KEY_PERSISTENCE_PATH elsewhere in this project:
    CLICKHOUSE_HOST      (default: localhost)
    CLICKHOUSE_PORT      (default: 8123)
    CLICKHOUSE_DB        (default: analytics)
    CLICKHOUSE_USER      (default: default)
    CLICKHOUSE_PASSWORD  (default: empty)

Run one locally with:
    docker run -d --name clickhouse -p 8123:8123 \
        -e CLICKHOUSE_DB=analytics clickhouse/clickhouse-server:24-alpine
"""

import os
import sqlite3
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analytics.warehouse import ClickHouseWarehouse

FACT_TABLE = "fact_transactions"
PAGE_SIZE = int(os.environ.get("SYNC_PAGE_SIZE", "10000"))


def build_warehouse() -> ClickHouseWarehouse:
    return ClickHouseWarehouse(
        host=os.environ.get("CLICKHOUSE_HOST", "localhost"),
        port=int(os.environ.get("CLICKHOUSE_PORT", "8123")),
        database=os.environ.get("CLICKHOUSE_DB", "analytics"),
        username=os.environ.get("CLICKHOUSE_USER", "default"),
        password=os.environ.get("CLICKHOUSE_PASSWORD", ""),
        secure=os.environ.get("CLICKHOUSE_SECURE", "0") == "1",
    )


def extract_new_transactions(ledger_db_path: str, since, limit: int = PAGE_SIZE):
    """
    Pulls transactions newer than `since` from the operational SQLite ledger,
    joined to their debit and credit entries -- the transactions table alone
    carries no account_id, only the ledger_entries rows tied to it do.

    ORDER BY created_at matters: the caller advances its watermark to the
    last row's timestamp, so an unordered result would leave the watermark
    past rows that were never read, skipping them permanently.

    `since` is compared as a STRING, and is the exact string this function
    returned last time. No parsing, so no timezone conversion can lose an
    offset -- the failure this project already hit once.
    """
    conn = sqlite3.connect(ledger_db_path)
    query = """
        SELECT t.rrn, t.amount_cents, t.created_at,
               debit.account_id  AS debit_account_id,
               credit.account_id AS credit_account_id
        FROM transactions t
        JOIN ledger_entries debit  ON debit.rrn  = t.rrn AND debit.entry_type  = 'debit'
        JOIN ledger_entries credit ON credit.rrn = t.rrn AND credit.entry_type = 'credit'
    """
    params = ()
    if since is not None:
        query += " WHERE t.created_at > ?"
        params = (since,)
    query += f" ORDER BY t.created_at ASC LIMIT {int(limit)}"

    rows = [
        {
            "rrn": rrn,
            "amount_cents": amount_cents,
            "transaction_ts": created_at,
            "created_at_raw": created_at,   # the verbatim watermark value
            "debit_account_id": debit_account_id,
            "credit_account_id": credit_account_id,
        }
        for rrn, amount_cents, created_at, debit_account_id, credit_account_id
        in conn.execute(query, params)
    ]
    conn.close()
    return rows


def main():
    if len(sys.argv) != 2:
        print("Usage: python3 analytics/sync_to_warehouse.py <ledger_db_path>")
        sys.exit(2)

    ledger_db_path = sys.argv[1]
    run_id = f"run-{uuid.uuid4().hex[:12]}"
    warehouse = build_warehouse()

    try:
        warehouse.ensure_schema()

        watermark = warehouse.get_watermark(FACT_TABLE)
        print(f"Warehouse holds data up to: {watermark or '(empty -- first run)'}")

        total = 0
        while True:
            rows = extract_new_transactions(ledger_db_path, watermark)
            if not rows:
                break

            loaded = warehouse.load_transactions(rows)
            total += loaded

            # Advance ONLY after the batch is confirmed in.
            watermark = rows[-1]["created_at_raw"]
            warehouse.set_watermark(FACT_TABLE, watermark, run_id, total)
            print(f"Loaded {loaded} row(s), watermark now {watermark}")

            if len(rows) < PAGE_SIZE:
                break

        print(f"Sync complete: {total} row(s) loaded this run, "
              f"{warehouse.count_transactions()} in the warehouse.")
        return 0

    except Exception as exc:
        # Non-zero exit is what a CronJob's alerting keys off, same as
        # ops/run_reconciliation.py. The watermark was not advanced past
        # anything unconfirmed, so the next run resumes cleanly.
        print(f"Sync FAILED: {exc!r}", file=sys.stderr)
        return 1

    finally:
        warehouse.close()


if __name__ == "__main__":
    sys.exit(main())
