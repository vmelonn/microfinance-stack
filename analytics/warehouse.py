"""
Analytics warehouse -- ClickHouse.

The architectural point of this layer is unchanged: a data warehouse is NOT
a replacement for the operational ledger. The ledger is optimized for "does
this account have the funds, right now" -- a few rows per transaction. A
warehouse is optimized for the opposite: "what was total transaction volume
by day over the last year" -- scanning millions of rows, rarely touching any
single one. Real systems export from the operational database INTO a
warehouse precisely because one engine being good at both jobs is rare.

WHAT CHANGED, AND WHY: this was Redshift.

RedshiftWarehouse was correct-shaped code that could never be run here --
no AWS account, no cluster, no credentials -- so it sat in exactly the same
position as AWSKeyManagementService: honest about being untestable, and
untested. ClickHouse self-hosts in a container, which moves this layer from
"probably right" to "proven by the test suite on every run". That is a
strictly better place for the one component whose job is to be trusted with
reporting numbers.

The swap was cheap because the DataWarehouse interface already existed. That
is the whole reason it existed.

THE LOADING MODEL IS THE OPPOSITE OF REDSHIFT'S, and it is worth being
explicit since the old docstring said the reverse. Redshift punishes
row-by-row INSERT and wants COPY FROM S3 -- meaning an S3 bucket, an IAM
role, a staging table, and MERGE grammar. ClickHouse wants large batched
INSERTs and needs none of that. Several hundred lines of planned
infrastructure simply do not exist here.

TWO TRAPS, both documented where they bite:
  1. ReplacingMergeTree deduplicates EVENTUALLY, not at insert time.
  2. A materialized view fires BEFORE that deduplication happens.
See ensure_schema() and load_transactions().
"""

from abc import ABC, abstractmethod
from datetime import datetime, timezone


# ClickHouse strongly prefers few large inserts to many small ones. Every
# INSERT creates a "part" on disk that background merges must consolidate;
# thousands of tiny inserts produce thousands of parts and eventually a
# "too many parts" error that stalls ingestion entirely.
DEFAULT_BATCH_SIZE = 50_000

FACT_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS fact_transactions (
    rrn                String,
    amount_cents       Int64,
    transaction_ts     DateTime64(3, 'UTC'),
    debit_account_id   LowCardinality(String),
    credit_account_id  LowCardinality(String),
    loaded_at          DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(loaded_at)
PARTITION BY toYYYYMM(transaction_ts)
ORDER BY (transaction_ts, rrn)
TTL toDateTime(transaction_ts) + INTERVAL 7 YEAR
"""
# ORDER BY (transaction_ts, rrn) does the job Redshift's SORTKEY did: every
# analytical query here is time-bounded, and the sparse primary index lets
# ClickHouse skip whole granules outside the range. `rrn` completes the key,
# which makes (ts, rrn) the DEDUPLICATION key -- both components are stable
# for a given transaction, since transaction_ts comes from the operational
# row's created_at and never changes, so re-loading the same RRN collapses
# correctly.
#
# PARTITION BY toYYYYMM makes the 7-year retention a metadata-only
# DROP PARTITION rather than a mass row delete.
#
# LowCardinality is dictionary encoding: these columns have few distinct
# values across millions of rows, and it is a large, free win.

WATERMARK_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS etl_watermark (
    table_name     String,
    last_loaded_ts String,
    last_run_id    String,
    rows_loaded    UInt64,
    updated_at     DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(updated_at)
ORDER BY table_name
"""
# last_loaded_ts is a String, and that is deliberate.
#
# It is the fix for the bug this project already hit once: comparing
# timestamps as text across two engines silently broke incremental sync,
# because Postgres's plain TIMESTAMP drops the UTC offset that SQLite's
# string format carries -- so every sync re-processed the same rows forever,
# saved from actual duplicates only by ON CONFLICT DO NOTHING. Storing the
# EXACT string the source emitted, and feeding that identical string back as
# the `since` parameter, removes the conversion entirely. No parse, nothing
# to lose in translation.

AGG_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS agg_daily_volume (
    day         Date,
    account_id  LowCardinality(String),
    txn_count   UInt64,
    total_cents Int64
)
ENGINE = SummingMergeTree
ORDER BY (day, account_id)
"""

MATERIALIZED_VIEW_DDL = """
CREATE MATERIALIZED VIEW IF NOT EXISTS mv_daily_volume TO agg_daily_volume AS
SELECT
    toDate(transaction_ts) AS day,
    debit_account_id       AS account_id,
    count()                AS txn_count,
    sum(amount_cents)      AS total_cents
FROM fact_transactions
GROUP BY day, account_id
"""
# The thing ClickHouse is genuinely good at and Redshift is not: daily volume
# becomes a single-digit-millisecond read regardless of fact table size,
# maintained incrementally at insert time.
#
# TRAP: this view fires on INSERT, BEFORE ReplacingMergeTree deduplicates.
# Re-loading a duplicate RRN double-counts here even though
# fact_transactions ends up correct. The watermark is therefore not merely an
# optimization -- it is what keeps these aggregates honest.


class DataWarehouse(ABC):
    """The seam. Kept even with one implementation, because it is what made
    replacing Redshift a contained change instead of a rewrite."""

    @abstractmethod
    def ensure_schema(self) -> None:
        ...

    @abstractmethod
    def get_watermark(self, table_name: str):
        """The newest source timestamp already confirmed loaded, or None."""
        ...

    @abstractmethod
    def set_watermark(self, table_name: str, last_ts: str, run_id: str, rows: int) -> None:
        ...

    @abstractmethod
    def load_transactions(self, rows: list) -> int:
        """rows: dicts with rrn, amount_cents, transaction_ts,
        debit_account_id, credit_account_id. Returns how many were loaded."""
        ...

    @abstractmethod
    def execute_query(self, sql: str, params: tuple = ()) -> list:
        ...


class ClickHouseWarehouse(DataWarehouse):
    """
    Uses clickhouse-connect, the official driver, over HTTP.

    HTTP rather than the native TCP protocol on purpose: it traverses an
    OpenShift Service and Route with no special handling, and it is far
    easier to debug with curl when something is wrong.
    """

    def __init__(self, host: str, port: int = 8123, database: str = "analytics",
                 username: str = "default", password: str = "", secure: bool = False):
        import clickhouse_connect

        self._clickhouse_connect = clickhouse_connect
        self._conn_kwargs = dict(
            host=host, port=port, database=database,
            username=username, password=password, secure=secure,
        )
        self._database = database
        self._client = None

    @property
    def client(self):
        if self._client is None:
            self._client = self._clickhouse_connect.get_client(**self._conn_kwargs)
        return self._client

    def ensure_schema(self) -> None:
        self.client.command(f"CREATE DATABASE IF NOT EXISTS {self._database}")
        for ddl in (FACT_TABLE_DDL, WATERMARK_TABLE_DDL, AGG_TABLE_DDL, MATERIALIZED_VIEW_DDL):
            self.client.command(ddl)

    def get_watermark(self, table_name: str = "fact_transactions"):
        # FINAL forces deduplication at read time. Without it a watermark
        # written twice could return the OLDER row, and a watermark that goes
        # backwards re-loads rows already present -- which the materialized
        # view would then double-count.
        result = self.client.query(
            "SELECT last_loaded_ts FROM etl_watermark FINAL WHERE table_name = {t:String}",
            parameters={"t": table_name},
        )
        return result.result_rows[0][0] if result.result_rows else None

    def set_watermark(self, table_name: str, last_ts: str, run_id: str, rows: int) -> None:
        self.client.insert(
            "etl_watermark",
            [[table_name, last_ts, run_id, rows, datetime.now(timezone.utc)]],
            column_names=["table_name", "last_loaded_ts", "last_run_id", "rows_loaded", "updated_at"],
        )

    def load_transactions(self, rows: list, batch_size: int = DEFAULT_BATCH_SIZE) -> int:
        if not rows:
            return 0

        now = datetime.now(timezone.utc)
        columns = ["rrn", "amount_cents", "transaction_ts",
                   "debit_account_id", "credit_account_id", "loaded_at"]
        loaded = 0

        for start in range(0, len(rows), batch_size):
            batch = rows[start:start + batch_size]
            self.client.insert(
                "fact_transactions",
                [
                    [
                        row["rrn"],
                        int(row["amount_cents"]),
                        _as_utc(row["transaction_ts"]),
                        row["debit_account_id"],
                        row["credit_account_id"],
                        now,
                    ]
                    for row in batch
                ],
                column_names=columns,
            )
            loaded += len(batch)

        return loaded

    def count_transactions(self) -> int:
        """FINAL, so this reflects post-deduplication reality rather than
        however many parts happen to be unmerged at this instant."""
        return self.client.query("SELECT count() FROM fact_transactions FINAL").result_rows[0][0]

    def execute_query(self, sql: str, params: tuple = ()) -> list:
        return self.client.query(sql, parameters=params).result_rows

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None


def _as_utc(value) -> datetime:
    """
    Source timestamps arrive as strings from SQLite. Anything without an
    explicit offset is treated as UTC -- never as local time, which would
    shift every row by the host's timezone and make reports disagree between
    machines.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

    text = str(value).strip().replace(" ", "T")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
