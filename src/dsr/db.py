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
    connect_args = {}
    if url.startswith("sqlite:///"):
        Path(url[len("sqlite:///") :]).parent.mkdir(parents=True, exist_ok=True)
        # A busy writer (e.g. two loader scripts run concurrently) otherwise
        # raises "database is locked" immediately instead of waiting -- this
        # happened for real running two bulk-load scripts against the same
        # file at once. 30s is generous enough for a commit to clear even
        # under load; each individual commit here is small.
        connect_args["timeout"] = 30
    # pool_pre_ping: test each pooled connection with a lightweight query
    # before handing it out, transparently reconnecting if it's gone stale.
    # Real bug this fixes: Neon (and managed Postgres generally) closes idle
    # connections server-side; app.py caches the Engine for the whole process
    # lifetime (st.cache_resource), so without this, the first query after
    # the app sat idle for a while got a dead connection from the pool and
    # raised OperationalError -- and stayed broken for every query after
    # that, since the pool had no way to know the connection was bad, until
    # someone manually rebooted the app to get a fresh Engine.
    return create_engine(url, future=True, connect_args=connect_args, pool_pre_ping=True)


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
