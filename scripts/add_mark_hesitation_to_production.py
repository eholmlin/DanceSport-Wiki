"""One-off: create the mark_hesitation table (Cre8tive Score's per-judge
"hesitation" flags, see dsr.models.MarkHesitation) on the production
Postgres database, which was created before that table existed.

Purely additive and idempotent: the table is only created if it is missing,
nothing existing is touched, and a second run reports "already there". If
production tracks its schema with Alembic (an alembic_version table) and is
exactly one step behind (at a0799bd18964), it is also advanced to this
table's revision so a future `alembic upgrade head` doesn't try to create
the table a second time.

Usage:
    DSR_DATABASE_URL=postgresql://... .venv/bin/python scripts/add_mark_hesitation_to_production.py
    DSR_DATABASE_URL=postgresql://... .venv/bin/python scripts/add_mark_hesitation_to_production.py --dry-run
"""
from __future__ import annotations

import argparse
import os

from sqlalchemy import create_engine, inspect, text

from dsr.models import MarkHesitation

PREVIOUS_REVISION = "a0799bd18964"
THIS_REVISION = "c3e1f5a7b9d2"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="report what would change without changing anything")
    args = parser.parse_args()

    pg_url = os.environ.get("DSR_DATABASE_URL")
    if not pg_url or not pg_url.startswith("postgres"):
        raise SystemExit("Set DSR_DATABASE_URL to the target Postgres URL before running this script.")

    engine = create_engine(pg_url, future=True)
    insp = inspect(engine)

    if insp.has_table("mark_hesitation"):
        print("mark_hesitation already exists in production -- nothing to create.")
    elif args.dry_run:
        print("--dry-run: would create the mark_hesitation table.")
    else:
        MarkHesitation.__table__.create(engine)
        print("Created the mark_hesitation table.")

    if insp.has_table("alembic_version"):
        with engine.connect() as conn:
            versions = [row[0] for row in conn.execute(text("select version_num from alembic_version"))]
        if versions == [PREVIOUS_REVISION]:
            if args.dry_run:
                print(f"--dry-run: would advance alembic_version {PREVIOUS_REVISION} -> {THIS_REVISION}.")
            else:
                with engine.begin() as conn:
                    conn.execute(
                        text("update alembic_version set version_num = :new where version_num = :old"),
                        {"new": THIS_REVISION, "old": PREVIOUS_REVISION},
                    )
                print(f"Advanced alembic_version {PREVIOUS_REVISION} -> {THIS_REVISION}.")
        else:
            print(f"alembic_version is {versions}; left as is.")
    else:
        print("Production has no alembic_version table; nothing to advance.")


if __name__ == "__main__":
    main()
