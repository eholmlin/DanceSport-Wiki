"""One-off copy of the app-relevant tables from the local SQLite database
into a Postgres database (e.g. a hosted Neon instance), for sharing a
Streamlit Cloud deployment with feedback testers.

Deliberately skips raw_document and resolution_queue: both are pipeline-
internal (fetch/parse provenance and entity-resolution review queue,
respectively), app.py never queries either, and raw_document alone is
~29k rows -- excluding them keeps the hosted copy well within a free-tier
storage cap for no loss of app functionality.

Run `alembic upgrade head` against the target DSR_DATABASE_URL first to
create the schema (this script only copies rows, it doesn't create tables).

Usage:
    DSR_DATABASE_URL=postgresql://... python scripts/migrate_sqlite_to_postgres.py
"""
from __future__ import annotations

import os
import time

from sqlalchemy import create_engine, insert, select, text
from sqlalchemy.orm import sessionmaker

from dsr.db import DEFAULT_DB_PATH
from dsr.models import (
    Organization,
    OrganizationAlias,
    Person,
    PersonAlias,
    Affiliation,
    Partnership,
    Competition,
    CompEvent,
    Entry,
    Round,
    Mark,
    Result,
    PairwiseOutcome,
    Rating,
)

# Dependency order: every model listed after its foreign-key targets.
MODELS_IN_ORDER = [
    Organization,
    Person,
    PersonAlias,
    OrganizationAlias,
    Affiliation,
    Partnership,
    Competition,
    CompEvent,
    Entry,
    Round,
    Mark,
    Result,
    PairwiseOutcome,
    Rating,
]

CHUNK_SIZE = 5000


def copy_table(sqlite_session, pg_engine, model) -> int:
    table = model.__table__
    total = 0
    with pg_engine.begin() as pg_conn:
        pg_conn.execute(text(f"TRUNCATE TABLE {table.name} CASCADE"))

    offset = 0
    while True:
        rows = sqlite_session.execute(select(table).offset(offset).limit(CHUNK_SIZE)).mappings().all()
        if not rows:
            break
        payload = [dict(r) for r in rows]
        with pg_engine.begin() as pg_conn:
            pg_conn.execute(insert(table), payload)
        total += len(rows)
        offset += CHUNK_SIZE
    return total


def reset_sequence(pg_engine, model) -> None:
    table = model.__table__
    if "id" not in table.columns:
        return
    with pg_engine.begin() as pg_conn:
        pg_conn.execute(
            text(
                f"SELECT setval(pg_get_serial_sequence('{table.name}', 'id'), "
                f"COALESCE((SELECT MAX(id) FROM {table.name}), 1), "
                f"(SELECT MAX(id) IS NOT NULL FROM {table.name}))"
            )
        )


def main() -> None:
    pg_url = os.environ.get("DSR_DATABASE_URL")
    if not pg_url or not pg_url.startswith("postgres"):
        raise SystemExit("Set DSR_DATABASE_URL to the target Postgres URL before running this script.")

    sqlite_engine = create_engine(f"sqlite:///{DEFAULT_DB_PATH}")
    sqlite_session = sessionmaker(bind=sqlite_engine, future=True)()
    pg_engine = create_engine(pg_url, future=True)

    for model in MODELS_IN_ORDER:
        t0 = time.time()
        n = copy_table(sqlite_session, pg_engine, model)
        reset_sequence(pg_engine, model)
        print(f"{model.__tablename__:20s} {n:>10,} rows in {time.time() - t0:.1f}s", flush=True)

    print("\nDone.")


if __name__ == "__main__":
    main()
