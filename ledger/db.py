import sqlite3

def _migrate_add_password_hash(conn: sqlite3.Connection):
    """
    CREATE TABLE IF NOT EXISTS only helps on a brand-new database -- it
    does nothing to a users table that already existed before password_hash
    was added to the schema (this project's own persistent ledger.db,
    among others). Without this, existing registered users would silently
    lose the ability to log in, or every startup would error out entirely.

    Existing rows get password_hash = '' (empty string), which the login
    endpoint treats as "no password set yet" rather than a valid hash --
    those accounts need a one-time password-set step before they can log
    in, but nothing about them or their ledger history is lost.
    """
    existing_columns = {row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
    if "password_hash" not in existing_columns:
        conn.execute("ALTER TABLE users ADD COLUMN password_hash TEXT NOT NULL DEFAULT ''")


def init_db(db_path: str):
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    
    with conn:
        # 1. Identity (The Human)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                full_name TEXT NOT NULL,
                cnic TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        _migrate_add_password_hash(conn)
        
        # 2. Ledger Buckets (The Money)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS accounts (
                account_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(user_id),
                type TEXT DEFAULT 'checking'
            )
        """)
        
        # 3. Financial Instruments (The Plastic)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cards (
                card_number TEXT PRIMARY KEY,
                account_id TEXT NOT NULL REFERENCES accounts(account_id),
                status TEXT DEFAULT 'active'
            )
        """)

        # 4. Transactions Tracker
        conn.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                rrn TEXT PRIMARY KEY,
                amount_cents INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 5. Ledger Entries (Now strictly tied to the 'accounts' table)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ledger_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rrn TEXT NOT NULL REFERENCES transactions(rrn),
                account_id TEXT NOT NULL REFERENCES accounts(account_id),
                entry_type TEXT NOT NULL CHECK (entry_type IN ('debit', 'credit')),
                amount_cents INTEGER NOT NULL
            )
        """)
    conn.close()

def get_connection(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn