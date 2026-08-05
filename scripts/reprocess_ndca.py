"""Re-parse and re-load every already-stored NDCA raw_document from disk --
no network access. Used to correct data after a parser bug fix (placements
were read from Dances[0] instead of the authoritative Round.Summary -- see
parse/ndca.py) without re-fetching anything, per the raw-first principle:
parsers are re-runnable against stored raw bytes.

Resumable: after each commit, the id of the last-processed RawDocument is
written to CHECKPOINT_PATH. On restart, documents up to and including that id
are skipped entirely (not just idempotently reprocessed) so an interruption
-- closing the laptop, killing the process -- costs at most one commit
interval of wall-clock time, not a restart from zero.

Usage:
    python scripts/reprocess_ndca.py
    python scripts/reprocess_ndca.py --cyis 261,798,627,904,303,1488,865
"""
from __future__ import annotations

import argparse
from pathlib import Path

from sqlalchemy import select

from dsr.db import get_session
from dsr.fetch.storage import load_raw_bytes
from dsr.load.ndca import load_event
from dsr.models import Competition, RawDocument
from dsr.parse.ndca import ParseError, parse_competitor_feed

CHECKPOINT_PATH = Path(__file__).resolve().parent.parent / "data" / ".ndca_reprocess_checkpoint"
COMMIT_EVERY = 200


def _read_checkpoint() -> int:
    if CHECKPOINT_PATH.exists():
        return int(CHECKPOINT_PATH.read_text().strip() or 0)
    return 0


def _write_checkpoint(doc_id: int) -> None:
    CHECKPOINT_PATH.write_text(str(doc_id))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--cyis",
        type=str,
        default=None,
        help="comma-separated competition source_codes to limit reprocessing to; default reprocesses everything "
        "(and uses the checkpoint file). A --cyis run ignores the checkpoint -- it's meant for small, targeted "
        "re-runs, not the full resumable sweep.",
    )
    args = parser.parse_args()
    cyi_filter = set(args.cyis.split(",")) if args.cyis else None

    session = get_session()

    competitions_by_source_code = {
        c.source_code: c for c in session.scalars(select(Competition).where(Competition.source == "ndca_premier")).all()
    }

    since_id = 0 if cyi_filter else _read_checkpoint()
    all_docs = session.scalars(
        select(RawDocument)
        .where(RawDocument.source == "ndca_premier", RawDocument.url.like("%&id=%"), RawDocument.id > since_id)
        .order_by(RawDocument.id)
    ).all()
    if cyi_filter:
        all_docs = [doc for doc in all_docs if doc.url.split("cyi=")[1].split("&")[0] in cyi_filter]
        print(f"Targeted reprocess: {len(all_docs)} documents across {len(cyi_filter)} competition(s).", flush=True)
    else:
        print(
            f"Resuming after checkpoint doc id={since_id}. {len(all_docs)} documents remaining to reprocess.",
            flush=True,
        )

    n_events = 0
    n_errors = 0
    last_id = since_id
    for i, doc in enumerate(all_docs):
        cyi = doc.url.split("cyi=")[1].split("&")[0]
        competition = competitions_by_source_code.get(cyi)
        if competition is not None:
            try:
                events = parse_competitor_feed(load_raw_bytes(doc))
            except ParseError:
                n_errors += 1
                events = []
            for event in events:
                load_event(session, competition, event)
                n_events += 1
        last_id = doc.id

        if (i + 1) % COMMIT_EVERY == 0:
            session.commit()
            if not cyi_filter:
                _write_checkpoint(last_id)
            print(f"  [{i + 1}/{len(all_docs)}] {n_events} events reprocessed so far (checkpoint={last_id})", flush=True)

    session.commit()
    if not cyi_filter:
        _write_checkpoint(last_id)
    print(f"\nDone. {n_events} event-loads reprocessed across {len(all_docs)} documents. {n_errors} parse errors.", flush=True)


if __name__ == "__main__":
    main()
