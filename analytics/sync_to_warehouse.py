"""
Incremental sync from the operational ledger (SQLite, optimized for
per-transaction reads/writes) into the analytics warehouse (optimized for
scanning everything at once). Same "runs on its own schedule, not per-
transaction" shape as ops/run_reconciliation.py -- this is what a
Kubernetes CronJob would invoke, e.g. once an hour.

Incremental, not a full reload: asks the warehouse for the newest
transaction_ts it already has, and only pulls rows from the operational
database newer than that. A full reload works fine at this project's toy
scale; it's a genuinely bad idea at real warehouse scale, so this is
built the way it would actually need to work, not just "however's easiest
for a demo."

Usage:
    python3 analytics/sync_to_warehouse.py <ledger_db_path>

Configuration via environment variables (same pattern as REDIS_URL,
HSM_KEY_PERSISTENCE_PATH elsewhere in this project):
    WAREHOUSE_TYPE=local (default) or redshift
    WAREHOUSE_DSN=postgresql://user:pass@host:port/dbname          (local)
    REDSHIFT_HOST / REDSHIFT_DB / REDSHIFT_USER / REDSHIFT_PASSWORD (redshift)
"""

import os
import sqlite3
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analytics.warehouse import LocalWarehouse, RedshiftWarehouse


def build_warehouse():
    warehouse_type = os.environ.get("WAREHOUSE_TYPE", "local")
    if warehouse_type == "redshift":
        return RedshiftWarehouse(
            host=os.environ["REDSHIFT_HOST"],
            database=os.environ["REDSHIFT_DB"],
            user=os.environ["REDSHIFT_USER"],
            password=os.environ["REDSHIFT_PASSWORD"],
        )
    dsn = os.environ.get("WAREHOUSE_DSN", "postgresql://practice_user:practice_pass123@127.0.0.1:5432/microfinance_warehouse")
    return LocalWarehouse(dsn)


def extract_new_transactions(ledger_db_path: str, since):
    """Pulls transactions newer than `since` from the operational SQLite
    ledger, joined with their debit/credit ledger entries to get both
    account IDs -- the operational transactions table alone doesn't carry
    account_id, only the ledger_entries rows tied to it do."""
    conn = sqlite3.connect(ledger_db_path)
    query = """
        SELECT t.rrn, t.amount_cents, t.created_at,
               debit.account_id AS debit_account_id,
               credit.account_id AS credit_account_id
        FROM transactions t
        JOIN ledger_entries debit ON debit.rrn = t.rrn AND debit.entry_type = 'debit'
        JOIN ledger_entries credit ON credit.rrn = t.rrn AND credit.entry_type = 'credit'
    """
    params = ()
    if since is not None:
        query += " WHERE t.created_at > ?"
        params = (since.isoformat(),)

    rows = []
    for rrn, amount_cents, created_at, debit_account_id, credit_account_id in conn.execute(query, params):
        # SQLite stores created_at as a string -- parse it back to a real datetime
        ts = datetime.fromisoformat(created_at) if isinstance(created_at, str) else created_at
        rows.append({
            "rrn": rrn, "amount_cents": amount_cents, "transaction_ts": ts,
            "debit_account_id": debit_account_id, "credit_account_id": credit_account_id,
        })
    conn.close()
    return rows


def main():
    if len(sys.argv) != 2:
        print("Usage: python3 analytics/sync_to_warehouse.py <ledger_db_path>")
        sys.exit(2)

    ledger_db_path = sys.argv[1]
    warehouse = build_warehouse()
    warehouse.ensure_schema()

    latest = warehouse.get_latest_loaded_timestamp()
    print(f"Warehouse currently has data up to: {latest or '(empty)'}")

    new_rows = extract_new_transactions(ledger_db_path, latest)
    print(f"Found {len(new_rows)} new transaction(s) to sync")

    loaded = warehouse.load_transactions(new_rows)
    print(f"Loaded {loaded} row(s) into the warehouse")


if __name__ == "__main__":
    main()
