"""Quick row-count snapshot of the app DB -- safe to run anytime, including
while scripts/bulk_load_season.py is mid-run (SQLite allows concurrent reads
while the writer holds its own connection open, and the loader commits after
every competition, so counts here reflect real, already-persisted progress).

Usage: python scripts/db_status.py
"""
from sqlalchemy import func, select

from dsr.db import get_session
from dsr.models import CompEvent, Competition, Entry, Mark, Partnership, Person, Result, ResolutionQueue, Round


def main() -> None:
    session = get_session()
    models = [Competition, CompEvent, Round, Person, Partnership, Entry, Result, Mark, ResolutionQueue]
    width = max(len(m.__tablename__) for m in models)
    for model in models:
        n = session.scalar(select(func.count()).select_from(model))
        print(f"{model.__tablename__:<{width}}  {n}")

    latest = session.scalars(
        select(Competition).order_by(Competition.id.desc()).limit(5)
    ).all()
    if latest:
        print("\nMost recently loaded competitions:")
        for c in reversed(latest):
            print(f"  [{c.id}] {c.name} ({c.start_date} to {c.end_date})")


if __name__ == "__main__":
    main()
