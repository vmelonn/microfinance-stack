"""
Entry point for a scheduled reconciliation run -- this is what a
Kubernetes CronJob's container would actually execute once a day, after
the switch's settlement file becomes available.

Usage:
    python3 ops/run_reconciliation.py <ledger_db_path> <settlement_json_path>

Exits with status 1 if anything doesn't reconcile cleanly, so a CronJob's
failure/alerting can key off the exit code without needing to parse output.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ledger.db import get_connection
from ops.reconciliation import reconcile


def main():
    if len(sys.argv) != 3:
        print("Usage: python3 ops/run_reconciliation.py <ledger_db_path> <settlement_json_path>")
        sys.exit(2)

    ledger_db_path, settlement_json_path = sys.argv[1], sys.argv[2]

    conn = get_connection(ledger_db_path)
    with open(settlement_json_path) as f:
        settlement_entries = json.load(f)

    result = reconcile(conn, settlement_entries)
    print(result.summary())

    if not result.is_clean:
        if result.only_in_ledger:
            print("Only in ledger (switch has no record):", result.only_in_ledger)
        if result.only_in_settlement:
            print("Only in settlement (we have no record):", result.only_in_settlement)
        if result.amount_mismatches:
            print("Amount mismatches:", result.amount_mismatches)
        sys.exit(1)

    print("Reconciliation clean.")
    sys.exit(0)


if __name__ == "__main__":
    main()
