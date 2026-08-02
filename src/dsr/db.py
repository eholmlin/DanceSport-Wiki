from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from dsr.models import Base

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "dsr.sqlite3"


def get_engine(db_path: Path | str | None = None):
    path = Path(db_path) if db_path else Path(os.environ.get("DSR_DB_PATH", DEFAULT_DB_PATH))
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{path}", future=True)
    return engine


def init_db(db_path: Path | str | None = None):
    engine = get_engine(db_path)
    Base.metadata.create_all(engine)
    return engine


def get_session(engine=None) -> Session:
    engine = engine or get_engine()
    factory = sessionmaker(bind=engine, future=True)
    return factory()
