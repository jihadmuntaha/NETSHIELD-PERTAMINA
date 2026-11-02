import os
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, declarative_base
from dotenv import load_dotenv

load_dotenv()

SQLALCHEMY_DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./netshield.db")

IS_SQLITE = SQLALCHEMY_DATABASE_URL.startswith("sqlite")

# connect_args={"check_same_thread": False} is required for SQLite in FastAPI multi-threaded context
# timeout=5 -> driver menunggu lock dilepas (bukan langsung "database is locked"). Dibuat
# pendek karena poller background menjalankan query sinkron di event loop.
engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args={"check_same_thread": False, "timeout": 5} if IS_SQLITE else {}
)

if IS_SQLITE:
    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):
        """
        WAL: pembaca (halaman dashboard/API) tidak memblokir penulis (poller background)
        dan sebaliknya. busy_timeout: tunggu hingga 5 detik saat ada penulis lain.
        synchronous=NORMAL aman untuk WAL dan mengurangi fsync per commit.
        """
        cur = dbapi_conn.cursor()
        try:
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=5000")
            cur.execute("PRAGMA synchronous=NORMAL")
        finally:
            cur.close()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


from sqlalchemy import text


def run_migrations():
    """Ensure missing columns in incident_logs and devices tables exist in SQLite database."""
    try:
        with engine.connect() as conn:
            # ── incident_logs migrations ─────────────────────────────────────
            res = conn.execute(text("PRAGMA table_info(incident_logs)"))
            columns = [row[1] for row in res.fetchall()]
            if columns:
                if "latency_ms" not in columns:
                    conn.execute(text("ALTER TABLE incident_logs ADD COLUMN latency_ms FLOAT"))
                if "title" not in columns:
                    conn.execute(text("ALTER TABLE incident_logs ADD COLUMN title VARCHAR(255)"))

            # ── devices — SNMP configuration columns ─────────────────────────
            res2 = conn.execute(text("PRAGMA table_info(devices)"))
            dev_cols = [row[1] for row in res2.fetchall()]
            if dev_cols:
                snmp_migrations = [
                    ("snmp_enabled",    "INTEGER NOT NULL DEFAULT 0"),
                    ("snmp_version",    "VARCHAR(10) NOT NULL DEFAULT '2c'"),
                    ("snmp_community",  "VARCHAR(100) NOT NULL DEFAULT 'public'"),
                    ("snmp_port",       "INTEGER NOT NULL DEFAULT 161"),
                    ("cpu_usage",       "FLOAT"),
                    ("memory_usage",    "FLOAT"),
                    ("uptime_str",      "VARCHAR(100)"),
                    ("sys_descr",       "TEXT"),
                    ("last_snmp_check", "DATETIME"),
                ]
                for col_name, col_def in snmp_migrations:
                    if col_name not in dev_cols:
                        conn.execute(text(f"ALTER TABLE devices ADD COLUMN {col_name} {col_def}"))

            conn.commit()
    except Exception as exc:
        print(f"[MIGRATION ERROR] {exc}")



def get_db():
    """Dependency generator for database sessions in FastAPI routes."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

