"""One-off fix: production's scheduled_heat sync failed with a
ForeignKeyViolation because today's newly-loaded heat lists reference
partnerships (and possibly people) created locally since the last full
sync, which production doesn't have yet.

Unlike migrate_sqlite_to_postgres.py, this does NOT truncate anything --
it only inserts the specific person/partnership rows that scheduled_heat
needs but production is missing, then re-runs the scheduled_heat sync
(which is a full truncate+reload of that one table, safe since nothing
depends on scheduled_heat).

Usage:
    DSR_DATABASE_URL=postgresql://... .venv/bin/python scripts/sync_missing_scheduled_heat_deps.py
"""
from __future__ import annotations

import os
import subprocess
import sys

from sqlalchemy import create_engine, insert, select, text
from sqlalchemy.orm import sessionmaker

from dsr.db import DEFAULT_DB_PATH
from dsr.models import Person, Partnership, ScheduledHeat


def main() -> None:
    pg_url = os.environ.get("DSR_DATABASE_URL")
    if not pg_url or not pg_url.startswith("postgres"):
        raise SystemExit("Set DSR_DATABASE_URL to the target Postgres URL before running this script.")

    sqlite_engine = create_engine(f"sqlite:///{DEFAULT_DB_PATH}")
    sqlite_session = sessionmaker(bind=sqlite_engine, future=True)()
    pg_engine = create_engine(pg_url, future=True)

    local_partnership_ids = {
        row[0] for row in sqlite_session.execute(select(ScheduledHeat.partnership_id)).all()
    }
    with pg_engine.connect() as pg_conn:
        existing_partnership_ids = {
            row[0] for row in pg_conn.execute(text("select id from partnership")).all()
        }
    missing_partnership_ids = local_partnership_ids - existing_partnership_ids
    print(f"{len(missing_partnership_ids)} partnerships referenced by scheduled_heat are missing in production")

    if missing_partnership_ids:
        missing_partnerships = sqlite_session.execute(
            select(Partnership).where(Partnership.id.in_(missing_partnership_ids))
        ).scalars().all()

        needed_person_ids = set()
        for p in missing_partnerships:
            needed_person_ids.update(pid for pid in (p.leader_id, p.follower_id, p.student_id) if pid is not None)

        with pg_engine.connect() as pg_conn:
            existing_person_ids = {
                row[0] for row in pg_conn.execute(text("select id from person")).all()
            }
        missing_person_ids = needed_person_ids - existing_person_ids
        print(f"{len(missing_person_ids)} people referenced by those partnerships are missing in production")

        if missing_person_ids:
            missing_people = sqlite_session.execute(
                select(Person).where(Person.id.in_(missing_person_ids))
            ).scalars().all()
            payload = [
                {c.name: getattr(p, c.name) for c in Person.__table__.columns} for p in missing_people
            ]
            with pg_engine.begin() as pg_conn:
                pg_conn.execute(insert(Person.__table__), payload)
            print(f"Inserted {len(payload)} missing person rows.")

        payload = [
            {c.name: getattr(p, c.name) for c in Partnership.__table__.columns} for p in missing_partnerships
        ]
        with pg_engine.begin() as pg_conn:
            pg_conn.execute(insert(Partnership.__table__), payload)
        print(f"Inserted {len(payload)} missing partnership rows.")

    print("\nRe-running the scheduled_heat sync now that its dependencies are present...")
    subprocess.run(
        [sys.executable, "scripts/migrate_sqlite_to_postgres.py", "--table", "scheduled_heat"],
        check=True,
    )


if __name__ == "__main__":
    main()
