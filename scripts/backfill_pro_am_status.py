"""Backfill Person.ndca_pro_am_status from already-stored NDCA raw documents
-- no network access. Each competitor-feed response's top-level
Result.Competitor carries a Pro_Am_Status for whichever person the feed was
fetched for ('A' amateur, 'P' professional, 'Y' the amateur/student half of
a pro-am pairing). NDCA ids are never trusted as external refs (see
resolve/entities.py), so this can't join by id -- it matches by exact
display_name instead, which is an acceptable approximation for a
supplementary "group an instructor's students vs. their amateur-couple
partners" feature, not a correctness-critical field.

For each name, the most common (mode) observed status wins, since the same
person can show slightly different statuses across registrations/years.

Resumable: same checkpoint pattern as reprocess_ndca.py.

Usage: python scripts/backfill_pro_am_status.py
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from sqlalchemy import select

from dsr.db import get_session
from dsr.fetch.storage import load_raw_bytes
from dsr.models import Person, RawDocument

CHECKPOINT_PATH = Path(__file__).resolve().parent.parent / "data" / ".pro_am_backfill_checkpoint"
COMMIT_EVERY = 2000


def _read_checkpoint() -> int:
    if CHECKPOINT_PATH.exists():
        return int(CHECKPOINT_PATH.read_text().strip() or 0)
    return 0


def _write_checkpoint(doc_id: int) -> None:
    CHECKPOINT_PATH.write_text(str(doc_id))


def main() -> None:
    session = get_session()

    since_id = _read_checkpoint()
    docs = session.scalars(
        select(RawDocument)
        .where(RawDocument.source == "ndca_premier", RawDocument.url.like("%&id=%"), RawDocument.id > since_id)
        .order_by(RawDocument.id)
    ).all()
    print(f"Resuming after checkpoint doc id={since_id}. {len(docs)} documents remaining to scan.", flush=True)

    # name -> Counter({'A': n, 'P': n, 'Y': n})
    status_counts: dict[str, Counter] = {}
    last_id = since_id
    for i, doc in enumerate(docs):
        try:
            payload = json.loads(load_raw_bytes(doc))
        except (json.JSONDecodeError, UnicodeDecodeError):
            last_id = doc.id
            continue
        comp = (payload.get("Result") or {}).get("Competitor") or {}
        name_parts = comp.get("Name")
        status = comp.get("Pro_Am_Status")
        if isinstance(name_parts, list) and status in ("A", "P", "Y"):
            name = " ".join(name_parts)
            status_counts.setdefault(name, Counter())[status] += 1
        last_id = doc.id

        if (i + 1) % COMMIT_EVERY == 0:
            _write_checkpoint(last_id)
            print(f"  scanned [{i + 1}/{len(docs)}] ({len(status_counts)} distinct names so far)", flush=True)

    _write_checkpoint(last_id)
    print(f"\nScan done. {len(status_counts)} distinct names with a Pro_Am_Status observed.", flush=True)

    updated = 0
    unmatched = 0
    for name, counts in status_counts.items():
        mode_status = counts.most_common(1)[0][0]
        people = list(session.scalars(select(Person).where(Person.display_name == name)).all())
        if not people:
            unmatched += 1
            continue
        for person in people:
            person.ndca_pro_am_status = mode_status
            if mode_status == "P":
                person.is_professional = True
            updated += 1
    session.commit()
    print(f"Updated {updated} Person rows ({unmatched} names had no matching Person).", flush=True)


if __name__ == "__main__":
    main()
