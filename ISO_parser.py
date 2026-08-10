import sqlite3

# Connect to your absolute database path
conn = sqlite3.connect("ledger.db")

print("--- ALL LEDGER ENTRIES ---")
for row in conn.execute("SELECT * FROM ledger_entries").fetchall():
    print(row)