"""Backfill CompEvent.style for already-loaded rows using the corrected
guess_style (see dsr.load.wdsf) -- no network access, pure in-DB update.

load_comp_event only sets style at creation time; fixing guess_style alone
has no effect on rows already loaded before the fix. Real case that
motivated this: 89% of comp_events had style=None, because guess_style
only recognized an explicit style-category word ("Latin"/"Standard"/
"Smooth"/"Rhythm"/"Ballroom") -- present on multi-dance/combined titles --
and most NDCA/Comp Manager titles instead name a single dance directly
with no category word at all.

Usage: python scripts/backfill_style.py
"""
from __future__ import annotations

from sqlalchemy import select

from dsr.db import get_session
from dsr.load.wdsf import guess_style
from dsr.models import CompEvent

# Ids grouped by computed style and updated via one "WHERE id IN (...)" per
# chunk, rather than one UPDATE statement per row -- at ~850k rows checked
# (~670k to actually update), row-at-a-time was the dominant cost.
CHUNK_SIZE = 1000


def main() -> None:
    session = get_session()

    rows = session.execute(select(CompEvent.id, CompEvent.raw_title).where(CompEvent.style.is_(None))).all()
    print(f"{len(rows)} comp_events with style=None to check.", flush=True)

    ids_by_style: dict[str, list[int]] = {}
    for comp_event_id, raw_title in rows:
        style = guess_style(raw_title)
        if style is not None:
            ids_by_style.setdefault(style, []).append(comp_event_id)

    updated = 0
    for style, ids in ids_by_style.items():
        for start in range(0, len(ids), CHUNK_SIZE):
            chunk = ids[start : start + CHUNK_SIZE]
            session.execute(CompEvent.__table__.update().where(CompEvent.id.in_(chunk)).values(style=style))
            updated += len(chunk)
        session.commit()
        print(f"  {style}: {len(ids)} comp_events updated", flush=True)

    print(f"\nDone. Updated {updated}/{len(rows)} comp_events.")


if __name__ == "__main__":
    main()
