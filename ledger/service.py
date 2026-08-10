"""
Ledger service: the only place in the whole stack that actually records
money moving.

record_purchase() is the core operation: given an RRN, a debit account, a
credit account, and an amount, it either creates both postings atomically,
or -- if this RRN has already been recorded -- does nothing and reports
that it was already recorded. There is no in-between state possible: the
transactions table's PRIMARY KEY on rrn guarantees the whole operation
either fully commits or fully fails, at the database level, not the
application level.
"""

import sqlite3
from datetime import datetime, timezone

from ledger.db import get_connection


def record_purchase(conn, rrn: str, debit_account: str, credit_account: str, amount_cents: int) -> dict:
    """
    Records a purchase as one journal entry: a debit on debit_account and a
    matching credit on credit_account, both tied to the same RRN.

    Returns {"status": "recorded", ...} on first insertion, or
    {"status": "already_recorded", ...} if this exact RRN was already
    processed -- the caller (Layer 5) never needs to distinguish these for
    its own logic, but it's useful for tests and logging to know which
    happened.

    IMPORTANT: SQLite raises the same sqlite3.IntegrityError for a
    duplicate primary key AND for a foreign key violation (e.g. an
    account_id that doesn't exist in the accounts table). Only the first
    is safe to treat as "already recorded" -- silently swallowing the
    second would mask a real bug (an invalid or unregistered account)
    behind a misleadingly reassuring "already_recorded" result. The two
    are distinguished by SQLite's own error message, which is stable
    across versions: a duplicate RRN always says exactly "UNIQUE
    constraint failed: transactions.rrn".
    """
    now = datetime.now(timezone.utc).isoformat()
    try:
        with conn:  # sqlite3's context manager commits on success, rolls back on exception
            conn.execute(
                "INSERT INTO transactions (rrn, amount_cents, created_at) VALUES (?, ?, ?)",
                (rrn, amount_cents, now),
            )
            conn.execute(
                "INSERT INTO ledger_entries (rrn, account_id, entry_type, amount_cents) VALUES (?, ?, 'debit', ?)",
                (rrn, debit_account, amount_cents),
            )
            conn.execute(
                "INSERT INTO ledger_entries (rrn, account_id, entry_type, amount_cents) VALUES (?, ?, 'credit', ?)",
                (rrn, credit_account, amount_cents),
            )
        return {"status": "recorded", "rrn": rrn, "amount_cents": amount_cents}
    except sqlite3.IntegrityError as e:
        if "UNIQUE constraint failed: transactions.rrn" in str(e):
            # The PRIMARY KEY on rrn rejected a duplicate -- this transaction
            # was already recorded. Nothing above this point was applied twice.
            return {"status": "already_recorded", "rrn": rrn}
        # Anything else (a foreign key violation, most likely: debit_account
        # or credit_account doesn't exist in the accounts table) is a real
        # error, not a safe no-op -- re-raise it rather than mislabeling it.
        raise


def get_balance(conn, account_id: str) -> int:
    """Balance = total credits - total debits, in cents, for one account."""
    row = conn.execute(
        """
        SELECT
            COALESCE(SUM(CASE WHEN entry_type = 'credit' THEN amount_cents ELSE 0 END), 0) -
            COALESCE(SUM(CASE WHEN entry_type = 'debit' THEN amount_cents ELSE 0 END), 0)
        FROM ledger_entries
        WHERE account_id = ?
        """,
        (account_id,),
    ).fetchone()
    return row[0]


def is_balanced(conn) -> bool:
    """
    Sanity check across the WHOLE ledger: total debits should always equal
    total credits. If this is ever False, something has gone genuinely
    wrong -- a partial write, a bug, or tampering.
    """
    row = conn.execute(
        """
        SELECT
            COALESCE(SUM(CASE WHEN entry_type = 'debit' THEN amount_cents ELSE 0 END), 0),
            COALESCE(SUM(CASE WHEN entry_type = 'credit' THEN amount_cents ELSE 0 END), 0)
        FROM ledger_entries
        """
    ).fetchone()
    total_debits, total_credits = row
    return total_debits == total_credits


def find_by_rrn(conn, rrn: str):
    row = conn.execute("SELECT rrn, amount_cents, created_at FROM transactions WHERE rrn = ?", (rrn,)).fetchone()
    if row is None:
        return None
    return {"rrn": row[0], "amount_cents": row[1], "created_at": row[2]}
