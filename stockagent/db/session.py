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
    return Path(settings.stockagent_db_path)


def _split_sql(sql: str) -> list[str]:
    return [s.strip() for s in sql.split(";") if s.strip()]
