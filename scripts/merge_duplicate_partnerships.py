"""Cleanup: merges any Partnership rows that are duplicates of each other --
same two people, same kind, leader and follower just swapped -- into one.

See dsr.resolve.entities.resolve_partnership_either_order's docstring for
how these came to exist: before that check was used by the results loader
too (not just the heat-list loader), a couple whose leader/follower order
was recorded differently across two sources (e.g. a results load vs. a
later heat-list load) got split across two Partnership rows, each carrying
only its own share of that couple's entries/scheduled heats -- visible as
duplicated rows on the dancer/heat-list pages. That's now fixed going
forward; this script cleans up any pairs that already exist from before.

Idempotent -- run it again and it finds nothing left to merge. Targets the
local SQLite DB by default; set DSR_DATABASE_URL to run against production
instead (same connection string used by the "Push ... to Production"
launchers).

Usage:
    .venv/bin/python scripts/merge_duplicate_partnerships.py
    .venv/bin/python scripts/merge_duplicate_partnerships.py --dry-run
    DSR_DATABASE_URL=postgresql://... .venv/bin/python scripts/merge_duplicate_partnerships.py
"""
from __future__ import annotations

import argparse

from sqlalchemy import func, select, update

from dsr.db import get_session
from dsr.models import Entry, Partnership, ScheduledHeat


def _row_count(session, partnership_id: int) -> int:
    n_entries = session.execute(select(func.count(Entry.id)).where(Entry.partnership_id == partnership_id)).scalar()
    n_heats = session.execute(
        select(func.count(ScheduledHeat.id)).where(ScheduledHeat.partnership_id == partnership_id)
    ).scalar()
    return n_entries + n_heats


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="print what would be merged without writing anything")
    args = parser.parse_args()

    session = get_session()
    partnerships = (
        session.execute(
            select(Partnership).where(Partnership.leader_id.isnot(None), Partnership.follower_id.isnot(None))
        )
        .scalars()
        .all()
    )

    by_pair: dict[tuple, list[Partnership]] = {}
    for p in partnerships:
        key = (frozenset({p.leader_id, p.follower_id}), p.kind)
        by_pair.setdefault(key, []).append(p)
    duplicate_groups = [group for group in by_pair.values() if len(group) > 1]

    print(f"{len(partnerships)} couple partnerships checked, {len(duplicate_groups)} duplicate group(s) found.")
    if not duplicate_groups:
        print("Nothing to merge.")
        return

    merged = entries_moved = heats_moved = heats_deleted = 0
    for group in duplicate_groups:
        # Keep whichever has the most related rows (ties broken by lower id, so the result is deterministic).
        group.sort(key=lambda p: (-_row_count(session, p.id), p.id))
        keep, *rest = group
        for remove in rest:
            print(f"  merge partnership {remove.id} (leader={remove.leader_id}, follower={remove.follower_id}) into {keep.id} (leader={keep.leader_id}, follower={keep.follower_id})")
            if args.dry_run:
                merged += 1
                continue

            entries_moved += session.execute(
                update(Entry).where(Entry.partnership_id == remove.id).values(partnership_id=keep.id)
            ).rowcount

            heats = session.execute(select(ScheduledHeat).where(ScheduledHeat.partnership_id == remove.id)).scalars().all()
            for heat in heats:
                clash = session.execute(
                    select(ScheduledHeat).where(
                        ScheduledHeat.partnership_id == keep.id,
                        ScheduledHeat.source == heat.source,
                        ScheduledHeat.source_event_id == heat.source_event_id,
                        ScheduledHeat.round_name == heat.round_name,
                    )
                ).scalars().first()
                if clash is not None:
                    session.delete(heat)
                    heats_deleted += 1
                else:
                    heat.partnership_id = keep.id
                    heats_moved += 1

            # Explicit flush before deleting the now-unreferenced partnership
            # -- there's no ORM relationship() between ScheduledHeat and
            # Partnership (just a bare FK column), so SQLAlchemy's unit of
            # work has no way to know the scheduled_heat reassignments above
            # must be written before this delete. SQLite doesn't enforce FK
            # constraints by default and let the wrong order slide silently;
            # Postgres (production) does, and failed with
            # "update or delete on table partnership violates foreign key
            # constraint scheduled_heat_partnership_id_fkey" the first time
            # this ran against it.
            session.flush()
            session.delete(remove)
            merged += 1

    if args.dry_run:
        print(f"\n--dry-run: {merged} partnership(s) would be merged. Nothing written.")
        return

    session.commit()
    print(
        f"\nMerged {merged} duplicate partnership(s): {entries_moved} entry row(s) moved, "
        f"{heats_moved} scheduled_heat row(s) moved, {heats_deleted} exact-duplicate scheduled_heat row(s) removed."
    )


if __name__ == "__main__":
    main()
