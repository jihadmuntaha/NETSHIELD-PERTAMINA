"""One-time migration: add resolved_at column to incident_logs table."""
import sqlite3

DB_PATH = r"c:\Users\Admin\Desktop\NETSHIELD-PERTAMINA\netshield.db"

conn = sqlite3.connect(DB_PATH)
cur = conn.cursor()

cur.execute("PRAGMA table_info(incident_logs)")
cols = [row[1] for row in cur.fetchall()]
print("Kolom incident_logs saat ini:", cols)

if "resolved_at" not in cols:
    cur.execute("ALTER TABLE incident_logs ADD COLUMN resolved_at DATETIME")
    conn.commit()
    print("OK: kolom resolved_at berhasil ditambahkan.")
else:
    print("SKIP: kolom resolved_at sudah ada.")

conn.close()
