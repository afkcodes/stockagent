from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from stockagent.config import settings

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"

_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def _ensure_db_dir(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)


def get_engine() -> Engine:
    global _engine, _SessionLocal
    if _engine is not None:
        return _engine

    db_path = Path(settings.stockagent_db_path)
    _ensure_db_dir(db_path)
    url = f"sqlite:///{db_path.as_posix()}"
    _engine = create_engine(url, future=True)

    @event.listens_for(_engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys = ON")
        cur.execute("PRAGMA journal_mode = WAL")
        cur.execute("PRAGMA synchronous = NORMAL")
        cur.close()

    _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    if _SessionLocal is None:
        get_engine()
    assert _SessionLocal is not None
    return _SessionLocal


@contextmanager
def session_scope():
    factory = get_session_factory()
    s = factory()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def init_db() -> Path:
    engine = get_engine()
    schema_sql = _SCHEMA_PATH.read_text()
    with engine.begin() as conn:
        for stmt in _split_sql(schema_sql):
            if stmt.strip():
                conn.execute(text(stmt))
    run_migrations()
    return Path(settings.stockagent_db_path)


# Idempotent ALTERs for columns that CREATE TABLE IF NOT EXISTS can't add to an
# already-existing table. Safe to run on every init / learn command.
_COLUMN_MIGRATIONS: list[tuple[str, str, str]] = [
    # (table, column, "ALTER ... ADD COLUMN ...")
    ("paper_trades", "initial_stop", "ALTER TABLE paper_trades ADD COLUMN initial_stop REAL"),
]


def _existing_columns(conn, table: str) -> set[str]:
    rows = conn.execute(text(f"PRAGMA table_info({table})")).mappings().all()
    return {r["name"] for r in rows}


def run_migrations() -> list[str]:
    """Apply additive column migrations idempotently. Returns the list applied."""
    engine = get_engine()
    applied: list[str] = []
    with engine.begin() as conn:
        for table, column, ddl in _COLUMN_MIGRATIONS:
            try:
                cols = _existing_columns(conn, table)
            except Exception:
                continue  # table doesn't exist yet; schema create will handle it
            if column not in cols:
                conn.execute(text(ddl))
                applied.append(f"{table}.{column}")
    return applied


def _split_sql(sql: str) -> list[str]:
    return [s.strip() for s in sql.split(";") if s.strip()]
