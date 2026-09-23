import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from dotenv import load_dotenv

load_dotenv()

SQLALCHEMY_DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./netshield.db")

# connect_args={"check_same_thread": False} is required for SQLite in FastAPI multi-threaded context
engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args={"check_same_thread": False} if SQLALCHEMY_DATABASE_URL.startswith("sqlite") else {}
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


from sqlalchemy import text


def run_migrations():
    """Ensure missing columns in incident_logs table exist in SQLite database."""
    try:
        with engine.connect() as conn:
            res = conn.execute(text("PRAGMA table_info(incident_logs)"))
            columns = [row[1] for row in res.fetchall()]
            if columns:
                if "latency_ms" not in columns:
                    conn.execute(text("ALTER TABLE incident_logs ADD COLUMN latency_ms FLOAT"))
                if "title" not in columns:
                    conn.execute(text("ALTER TABLE incident_logs ADD COLUMN title VARCHAR(255)"))
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

