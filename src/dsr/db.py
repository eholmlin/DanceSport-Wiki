from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from dsr.models import Base

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "dsr.sqlite3"


def resolve_database_url(db_path_or_url: Path | str | None = None) -> str:
    """Single source of truth for the DB connection string, shared with
    migrations/env.py so `alembic upgrade head` always targets the same
    database the app would. Postgres-ready: set DSR_DATABASE_URL (any
    SQLAlchemy URL, e.g. postgresql://...) to move off SQLite without code
    changes -- see docs re M2 Postgres migration being deferred pending an
    available Postgres instance.
    """
    if db_path_or_url is not None:
        text = str(db_path_or_url)
        return text if "://" in text else f"sqlite:///{text}"

    env_url = os.environ.get("DSR_DATABASE_URL")
    if env_url:
        return env_url

    path = Path(os.environ.get("DSR_DB_PATH", DEFAULT_DB_PATH))
    return f"sqlite:///{path}"


def get_engine(db_path: Path | str | None = None):
    url = resolve_database_url(db_path)
    if url.startswith("sqlite:///"):
        Path(url[len("sqlite:///") :]).parent.mkdir(parents=True, exist_ok=True)
    return create_engine(url, future=True)


def init_db(db_path: Path | str | None = None):
    """Dev/test convenience: create_all from current models. Once Alembic
    migrations exist, prefer `alembic upgrade head` for anything that needs to
    persist across schema changes -- create_all has no notion of migrations."""
    engine = get_engine(db_path)
    Base.metadata.create_all(engine)
    return engine


def get_session(engine=None) -> Session:
    engine = engine or get_engine()
    factory = sessionmaker(bind=engine, future=True)
    return factory()
