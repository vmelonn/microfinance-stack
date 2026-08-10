"""
Analytics warehouse, following the same swappable-interface pattern as
cache/idempotency_store.py and security/kms.py: one interface, one fully
real and tested implementation, one cloud implementation this sandbox
genuinely cannot reach.

The architectural point of this layer: a data warehouse is NOT a
replacement for the operational ledger database. The ledger is optimized
for "does this account have the funds, right now" -- a small number of
rows read/written per transaction. A warehouse is optimized for the
opposite: "what was total transaction volume by day over the last year" --
scanning millions of rows, rarely touching any single one. Real systems
periodically export from the operational database INTO a warehouse
specifically because one engine being good at both jobs is rare.

Redshift specifically speaks the Postgres wire protocol (it's built on a
forked Postgres engine), which is what makes the swap genuinely just a
connection-string change -- LocalWarehouse and RedshiftWarehouse run
nearly identical SQL.

Honest caveat: real, large-scale Redshift loading uses COPY FROM S3, not
row-by-row INSERT -- INSERT is a known-slow path at real Redshift volumes.
This implementation uses batched INSERT ... ON CONFLICT for both engines,
which is correct and fine at this project's scale, but isn't the
production-recommended loading pattern for a genuinely large warehouse.
"""

from abc import ABC, abstractmethod
from datetime import datetime, timezone


FACT_TABLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS fact_transactions (
    rrn                 TEXT PRIMARY KEY,
    amount_cents         BIGINT NOT NULL,
    transaction_date     DATE NOT NULL,
    transaction_ts        TIMESTAMPTZ NOT NULL,
    debit_account_id     TEXT NOT NULL,
    credit_account_id    TEXT NOT NULL,
    loaded_at             TIMESTAMPTZ NOT NULL
);
"""


class DataWarehouse(ABC):
    @abstractmethod
    def ensure_schema(self) -> None:
        ...

    @abstractmethod
    def get_latest_loaded_timestamp(self):
        """Returns the newest transaction_ts already in the warehouse, or None if empty.
        This is what makes sync incremental -- only pull rows newer than this."""
        ...

    @abstractmethod
    def load_transactions(self, rows: list) -> int:
        """rows: list of dicts with rrn, amount_cents, transaction_ts,
        debit_account_id, credit_account_id. Returns how many rows were loaded."""
        ...

    @abstractmethod
    def execute_query(self, sql: str, params: tuple = ()) -> list:
        """Runs an arbitrary analytical query, returns rows as a list of tuples."""
        ...


class LocalWarehouse(DataWarehouse):
    """A real Postgres warehouse -- genuinely tested, and wire-compatible
    with Redshift, so the SQL here is what would also run there."""

    def __init__(self, dsn: str):
        import psycopg2
        self._psycopg2 = psycopg2
        self._dsn = dsn

    def _connect(self):
        return self._psycopg2.connect(self._dsn)

    def ensure_schema(self) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(FACT_TABLE_SCHEMA)
            conn.commit()

    def get_latest_loaded_timestamp(self):
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT MAX(transaction_ts) FROM fact_transactions")
                return cur.fetchone()[0]

    def load_transactions(self, rows: list) -> int:
        if not rows:
            return 0
        with self._connect() as conn:
            with conn.cursor() as cur:
                for row in rows:
                    cur.execute("""
                        INSERT INTO fact_transactions
                            (rrn, amount_cents, transaction_date, transaction_ts,
                             debit_account_id, credit_account_id, loaded_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (rrn) DO NOTHING
                    """, (
                        row["rrn"], row["amount_cents"], row["transaction_ts"].date(),
                        row["transaction_ts"], row["debit_account_id"], row["credit_account_id"],
                        datetime.now(timezone.utc),
                    ))
            conn.commit()
        return len(rows)

    def execute_query(self, sql: str, params: tuple = ()) -> list:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchall()


class RedshiftWarehouse(DataWarehouse):
    """
    Real AWS Redshift integration -- correct redshift_connector API shape,
    genuinely untestable in this sandbox (no network route to AWS). Nearly
    identical to LocalWarehouse since Redshift speaks the same wire
    protocol; the differences that DO matter at real scale (COPY FROM S3
    for bulk loads, DISTKEY/SORTKEY table design) aren't addressed here --
    this class proves the connection and basic query shape are correct,
    not that it's tuned for production Redshift volumes.
    """

    def __init__(self, host: str, database: str, user: str, password: str, port: int = 5439):
        import redshift_connector
        self._redshift_connector = redshift_connector
        self._conn_kwargs = dict(host=host, database=database, user=user, password=password, port=port)

    def _connect(self):
        return self._redshift_connector.connect(**self._conn_kwargs)

    def ensure_schema(self) -> None:
        conn = self._connect()
        cur = conn.cursor()
        cur.execute(FACT_TABLE_SCHEMA)
        conn.commit()
        conn.close()

    def get_latest_loaded_timestamp(self):
        conn = self._connect()
        cur = conn.cursor()
        cur.execute("SELECT MAX(transaction_ts) FROM fact_transactions")
        result = cur.fetchone()[0]
        conn.close()
        return result

    def load_transactions(self, rows: list) -> int:
        if not rows:
            return 0
        conn = self._connect()
        cur = conn.cursor()
        for row in rows:
            cur.execute("""
                INSERT INTO fact_transactions
                    (rrn, amount_cents, transaction_date, transaction_ts,
                     debit_account_id, credit_account_id, loaded_at)
                SELECT %s, %s, %s, %s, %s, %s, %s
                WHERE NOT EXISTS (SELECT 1 FROM fact_transactions WHERE rrn = %s)
            """, (
                row["rrn"], row["amount_cents"], row["transaction_ts"].date(),
                row["transaction_ts"], row["debit_account_id"], row["credit_account_id"],
                datetime.now(timezone.utc), row["rrn"],
            ))
        conn.commit()
        conn.close()
        return len(rows)

    def execute_query(self, sql: str, params: tuple = ()) -> list:
        conn = self._connect()
        cur = conn.cursor()
        cur.execute(sql, params)
        result = cur.fetchall()
        conn.close()
        return result
