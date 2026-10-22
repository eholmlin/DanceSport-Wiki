"""Incremental sync of competition results from local SQLite to production
Postgres, for competitions whose results changed since the last sync.
Covers every source in SYNCED_SOURCES (NDCA Premier and Cre8tive Score).

Unlike migrate_sqlite_to_postgres.py, this never TRUNCATEs anything: every
row is upserted (inserted if new, updated in place if it already exists,
matched by primary key) and only rows belonging to competitions whose
Competition.source_updated_at differs from what's already in production get
touched at all. Safe to run as often as you like -- a competition already
up to date in production is skipped entirely.

A changed competition can reference brand-new people/partnerships/
organizations (new competitors NDCA hasn't been seen before) that
production doesn't have yet -- those are inserted first (never updated,
since an already-known person/partnership/organization is left alone here),
same approach as sync_missing_scheduled_heat_deps.py, before the
competition's own comp_event/round/entry/result/mark rows are upserted.

Usage:
    DSR_DATABASE_URL=postgresql://... .venv/bin/python scripts/sync_results_to_production.py
    DSR_DATABASE_URL=postgresql://... .venv/bin/python scripts/sync_results_to_production.py --dry-run
"""
from __future__ import annotations

import argparse
import datetime as dt
import os

from sqlalchemy import bindparam, create_engine, delete, inspect, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import sessionmaker

from dsr.db import DEFAULT_DB_PATH
from dsr.models import (
    Competition,
    CompEvent,
    Entry,
    Mark,
    MarkHesitation,
    Organization,
    Partnership,
    Person,
    Result,
    Round,
)

CHUNK_SIZE = 2000

# Sources whose results this script syncs. Each must set
# Competition.source_updated_at to something that changes when its results
# do (NDCA: Publish_Dates.Results; Cre8tive: resultsPublishedAt) -- that
# value is the change detector.
SYNCED_SOURCES = ("ndca_premier", "cre8tive")

# SQLite rejects a query with too many "?" placeholders (SQLITE_MAX_VARIABLE_NUMBER,
# as low as 999 on some builds), so any WHERE ... IN (...) over an id list that can
# grow large (e.g. every comp_event for a first-time full sync) has to be chunked.
SQLITE_IN_CHUNK_SIZE = 500


def select_in_chunks(sqlite, model, column, ids: list) -> list:
    """Run `select(model).where(column.in_(ids))` in batches small enough for
    SQLite's bound-parameter limit, and concatenate the results."""
    if not ids:
        return []
    ids = list(ids)
    rows = []
    for i in range(0, len(ids), SQLITE_IN_CHUNK_SIZE):
        chunk = ids[i : i + SQLITE_IN_CHUNK_SIZE]
        rows.extend(sqlite.execute(select(model).where(column.in_(chunk))).scalars().all())
    return rows


def upsert_by_pk(pg_conn, model, rows: list[dict], pk_cols: list[str]) -> int:
    """INSERT ... ON CONFLICT (pk_cols) DO UPDATE for every other column.
    A no-op (0 rows) call is fine -- callers don't need to special-case an
    empty list."""
    if not rows:
        return 0
    table = model.__table__
    update_cols = [c.name for c in table.columns if c.name not in pk_cols]
    total = 0
    for i in range(0, len(rows), CHUNK_SIZE):
        chunk = rows[i : i + CHUNK_SIZE]
        stmt = pg_insert(table).values(chunk)
        stmt = stmt.on_conflict_do_update(
            index_elements=pk_cols, set_={c: getattr(stmt.excluded, c) for c in update_cols}
        )
        pg_conn.execute(stmt)
        total += len(chunk)
    return total


def insert_missing_only(pg_conn, model, rows: list[dict]) -> int:
    """Plain INSERT for rows whose id the caller has already confirmed is
    missing in production -- used for person/partnership/organization,
    which this script never updates once they exist (an already-known
    person isn't expected to change via a results reload)."""
    if not rows:
        return 0
    table = model.__table__
    total = 0
    for i in range(0, len(rows), CHUNK_SIZE):
        chunk = rows[i : i + CHUNK_SIZE]
        pg_conn.execute(table.insert(), chunk)
        total += len(chunk)
    return total


def comparable_timestamp(value: dt.datetime | None) -> dt.datetime | None:
    """Normalize a source_updated_at for equality comparison across engines.
    SQLite has no real timezone support and always hands back naive
    datetimes for this column, while Postgres returns timezone-aware ones --
    comparing them directly with != is always True even for the same
    instant, which made every competition look changed. Converting aware
    values to naive UTC first makes the comparison meaningful."""
    if value is not None and value.tzinfo is not None:
        value = value.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return value


def row_to_dict(model, obj) -> dict:
    return {c.name: getattr(obj, c.name) for c in model.__table__.columns}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="print what would sync without writing anything")
    parser.add_argument(
        "--force",
        type=str,
        default=None,
        help="comma-separated source_code(s) (NDCA's cyi, e.g. '455', or a Cre8tive competition id) to sync regardless of the "
        "source_updated_at comparison -- for a competition whose production row already carries a "
        "source_updated_at that matches local without the results actually being there (e.g. its row "
        "was inserted by sync_missing_scheduled_heat_deps.py as a scheduled_heat FK dependency, "
        "copying local's value verbatim, before local's own value was corrected)",
    )
    args = parser.parse_args()
    forced_codes = set(args.force.split(",")) if args.force else set()

    pg_url = os.environ.get("DSR_DATABASE_URL")
    if not pg_url or not pg_url.startswith("postgres"):
        raise SystemExit("Set DSR_DATABASE_URL to the target Postgres URL before running this script.")

    sqlite_engine = create_engine(f"sqlite:///{DEFAULT_DB_PATH}")
    sqlite = sessionmaker(bind=sqlite_engine, future=True)()
    pg_engine = create_engine(pg_url, future=True)

    local_comps = {
        c.id: c
        for c in sqlite.execute(select(Competition).where(Competition.source.in_(SYNCED_SOURCES))).scalars().all()
    }
    with pg_engine.connect() as pg_conn:
        prod_updated_at = dict(
            pg_conn.execute(
                text("select id, source_updated_at from competition where source in :sources").bindparams(
                    bindparam("sources", expanding=True)
                ),
                {"sources": list(SYNCED_SOURCES)},
            ).all()
        )

    changed_ids = [
        cid
        for cid, comp in local_comps.items()
        if comparable_timestamp(prod_updated_at.get(cid)) != comparable_timestamp(comp.source_updated_at)
        or comp.source_code in forced_codes
    ]
    print(f"{len(local_comps)} local competitions, {len(changed_ids)} need syncing to production.")
    if not changed_ids:
        print("Nothing to do.")
        return

    for comp in sorted((local_comps[cid] for cid in changed_ids), key=lambda c: c.name):
        print(f"  [{comp.source_code}] {comp.name} ({comp.start_date})")

    comp_events = select_in_chunks(sqlite, CompEvent, CompEvent.competition_id, changed_ids)
    comp_event_ids = [ce.id for ce in comp_events]

    entries = select_in_chunks(sqlite, Entry, Entry.competition_id, changed_ids)
    entry_ids = [e.id for e in entries]

    rounds = select_in_chunks(sqlite, Round, Round.comp_event_id, comp_event_ids)
    round_ids = [r.id for r in rounds]

    results = select_in_chunks(sqlite, Result, Result.comp_event_id, comp_event_ids)
    marks = select_in_chunks(sqlite, Mark, Mark.round_id, round_ids)
    hesitations = select_in_chunks(sqlite, MarkHesitation, MarkHesitation.round_id, round_ids)

    print(
        f"\n{len(comp_events)} comp_events, {len(entries)} entries, {len(rounds)} rounds, "
        f"{len(results)} results, {len(marks)} marks, {len(hesitations)} hesitation flags to upsert."
    )

    # mark_hesitation arrived after the rest of the production schema; fail
    # before writing anything (rather than half-sync and stamp the
    # competition as done) if there are flags to sync and no table for them.
    has_hesitation_table = inspect(pg_engine).has_table("mark_hesitation")
    if hesitations and not has_hesitation_table:
        raise SystemExit(
            "Production has no mark_hesitation table yet. Run 'Add Cre8tive Table to Production.command' "
            "(scripts/add_mark_hesitation_to_production.py) once, then re-run this sync."
        )

    needed_partnership_ids = {e.partnership_id for e in entries if e.partnership_id is not None}
    needed_organization_ids = {e.organization_id for e in entries if e.organization_id is not None}

    with pg_engine.connect() as pg_conn:
        existing_partnership_ids = (
            {row[0] for row in pg_conn.execute(text("select id from partnership")).all()}
        )
    missing_partnership_ids = needed_partnership_ids - existing_partnership_ids
    missing_partnerships = select_in_chunks(sqlite, Partnership, Partnership.id, list(missing_partnership_ids))

    needed_person_ids = {
        pid
        for p in missing_partnerships
        for pid in (p.leader_id, p.follower_id, p.student_id)
        if pid is not None
    }
    needed_person_ids |= {m.judge_person_id for m in marks if m.judge_person_id is not None}
    needed_person_ids |= {h.judge_person_id for h in hesitations}
    with pg_engine.connect() as pg_conn:
        existing_person_ids = {row[0] for row in pg_conn.execute(text("select id from person")).all()}
    missing_person_ids = needed_person_ids - existing_person_ids
    missing_people = select_in_chunks(sqlite, Person, Person.id, list(missing_person_ids))

    with pg_engine.connect() as pg_conn:
        existing_org_ids = {row[0] for row in pg_conn.execute(text("select id from organization")).all()}
    missing_organization_ids = needed_organization_ids - existing_org_ids
    missing_organizations = select_in_chunks(sqlite, Organization, Organization.id, list(missing_organization_ids))

    print(
        f"\n{len(missing_people)} new people, {len(missing_organizations)} new organizations, "
        f"{len(missing_partnerships)} new partnerships referenced but missing in production."
    )

    if args.dry_run:
        print("\n--dry-run: nothing written.")
        return

    with pg_engine.begin() as pg_conn:
        # Competition first: a competition new to production (Cre8tive ones
        # are -- NDCA's already arrive via the heat-list sync) must exist
        # before its comp_event rows can reference it. This is one
        # transaction, so a failure further down rolls this back too and the
        # next run still sees the competition as unsynced.
        n = upsert_by_pk(
            pg_conn,
            Competition,
            [row_to_dict(Competition, local_comps[cid]) for cid in changed_ids],
            ["id"],
        )
        print(f"Upserted {n} competition rows (so future runs see these as already synced).")

        n = insert_missing_only(pg_conn, Person, [row_to_dict(Person, p) for p in missing_people])
        print(f"Inserted {n} new person rows.")
        n = insert_missing_only(
            pg_conn, Organization, [row_to_dict(Organization, o) for o in missing_organizations]
        )
        print(f"Inserted {n} new organization rows.")
        n = insert_missing_only(
            pg_conn, Partnership, [row_to_dict(Partnership, p) for p in missing_partnerships]
        )
        print(f"Inserted {n} new partnership rows.")

        n = upsert_by_pk(pg_conn, CompEvent, [row_to_dict(CompEvent, ce) for ce in comp_events], ["id"])
        print(f"Upserted {n} comp_event rows.")
        n = upsert_by_pk(pg_conn, Entry, [row_to_dict(Entry, e) for e in entries], ["id"])
        print(f"Upserted {n} entry rows.")
        n = upsert_by_pk(pg_conn, Round, [row_to_dict(Round, r) for r in rounds], ["id"])
        print(f"Upserted {n} round rows.")
        n = upsert_by_pk(
            pg_conn, Result, [row_to_dict(Result, r) for r in results], ["comp_event_id", "entry_id"]
        )
        print(f"Upserted {n} result rows.")
        n = upsert_by_pk(pg_conn, Mark, [row_to_dict(Mark, m) for m in marks], ["id"])
        print(f"Upserted {n} mark rows.")

        if has_hesitation_table:
            # Replace, don't upsert: a reload re-creates a round's flags with
            # new ids, and the old production rows would collide with them on
            # the table's (round, entry, judge, dance) unique key.
            hesitation_table = MarkHesitation.__table__
            for i in range(0, len(round_ids), CHUNK_SIZE):
                pg_conn.execute(
                    delete(hesitation_table).where(hesitation_table.c.round_id.in_(round_ids[i : i + CHUNK_SIZE]))
                )
            n = insert_missing_only(pg_conn, MarkHesitation, [row_to_dict(MarkHesitation, h) for h in hesitations])
            print(f"Replaced hesitation rows: {n} inserted.")

    print("\nDone.")


if __name__ == "__main__":
    main()
