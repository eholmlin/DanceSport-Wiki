"""One-off check: compare per-competition row counts between local SQLite
and production Postgres for every results-bearing table, to directly
verify production actually matches local rather than trusting a sync
script's own "upserted N rows" output -- the exact kind of gap that let
Galaxy Dance Festival's results silently sit un-synced despite the push
script reporting "Nothing to do."

One aggregate GROUP BY query per table per database (not one query per
competition), so this stays fast even across ~400 competitions.

Usage:
    DSR_DATABASE_URL=postgresql://... .venv/bin/python scripts/verify_production_sync.py
"""
from __future__ import annotations

import os

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from dsr.db import DEFAULT_DB_PATH

TABLES = [
    ("comp_event", "competition_id", "id"),
    ("entry", "competition_id", "id"),
    ("round", "comp_event_id", "id"),  # counted per comp_event, rolled up to competition below
    ("result", "comp_event_id", "comp_event_id"),  # comp_event_id doubles as the join key here
    ("mark", "round_id", "round_id"),
]


def counts_by_competition(conn, is_sqlite: bool) -> dict[str, dict[int, int]]:
    """Returns {table_name: {competition_id: row_count}}."""
    out: dict[str, dict[int, int]] = {}

    def q(sql: str) -> list[tuple]:
        return list(conn.execute(text(sql)).all())

    out["comp_event"] = dict(q("select competition_id, count(*) from comp_event group by competition_id"))
    out["entry"] = dict(q("select competition_id, count(*) from entry group by competition_id"))
    out["round"] = dict(
        q(
            "select ce.competition_id, count(*) from round r "
            "join comp_event ce on r.comp_event_id = ce.id group by ce.competition_id"
        )
    )
    out["result"] = dict(
        q(
            "select ce.competition_id, count(*) from result res "
            "join comp_event ce on res.comp_event_id = ce.id group by ce.competition_id"
        )
    )
    out["mark"] = dict(
        q(
            "select ce.competition_id, count(*) from mark m "
            "join round r on m.round_id = r.id "
            "join comp_event ce on r.comp_event_id = ce.id group by ce.competition_id"
        )
    )
    return out


def main() -> None:
    pg_url = os.environ.get("DSR_DATABASE_URL")
    if not pg_url or not pg_url.startswith("postgres"):
        raise SystemExit("Set DSR_DATABASE_URL to the target Postgres URL before running this script.")

    sqlite_engine = create_engine(f"sqlite:///{DEFAULT_DB_PATH}")
    pg_engine = create_engine(pg_url, future=True)

    with sqlite_engine.connect() as conn:
        local = counts_by_competition(conn, is_sqlite=True)
    with pg_engine.connect() as conn:
        prod = counts_by_competition(conn, is_sqlite=False)

    # Competition names for readable output, local side only (production
    # should have the same rows if everything's in sync, and this is just
    # for display).
    with sqlite_engine.connect() as conn:
        names = dict(conn.execute(text("select id, name from competition")).all())

    all_comp_ids = set()
    for table_counts in local.values():
        all_comp_ids.update(table_counts.keys())

    mismatches = []
    for cid in sorted(all_comp_ids):
        row_mismatch = {}
        for table_name in ("comp_event", "entry", "round", "result", "mark"):
            local_n = local[table_name].get(cid, 0)
            prod_n = prod[table_name].get(cid, 0)
            if local_n != prod_n:
                row_mismatch[table_name] = (local_n, prod_n)
        if row_mismatch:
            mismatches.append((cid, names.get(cid, "?"), row_mismatch))

    print(f"Checked {len(all_comp_ids)} competitions with results locally.")
    if not mismatches:
        print("All match -- production's row counts equal local's for every competition, every table.")
        return

    print(f"\n{len(mismatches)} competition(s) with a mismatch:")
    for cid, name, row_mismatch in mismatches:
        print(f"  [{cid}] {name}")
        for table_name, (local_n, prod_n) in row_mismatch.items():
            print(f"      {table_name}: local={local_n} prod={prod_n}")


if __name__ == "__main__":
    main()
