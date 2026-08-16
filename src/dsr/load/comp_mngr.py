"""Load stage glue for Comp Manager (comp-mngr.com).

The heavy lifting (rounds/entries/results upserts, entity resolution,
marks) is identical to WDSF's and NDCA's and lives in dsr.load.wdsf --
those functions are already source-parameterized and operate on the same
staging dataclasses, so they're reused directly here. Only comp_event
creation needed a thin Comp-Manager-specific version, since
StagingCompEventRef carries WDSF-specific page URLs this source doesn't
have (same reasoning as dsr.load.ndca).

One real difference from NDCA/WDSF: judges are competition-wide, not
per-event (there's no per-event officials list in the source data -- see
dsr.parse.comp_mngr.parse_judges, which reads the one "List of Judges"
legend for the whole competition). So load_officials is called once per
competition by the caller (see scripts/bulk_load_comp_mngr.py), not once
per event, and the resulting judge_letter_to_person_id map is threaded
into every load_event call.

Also unlike NDCA/WDSF, a single event's rounds (recall + final) arrive as
*separate* CompMngrEventData sharing one raw_title (see
dsr.parse.comp_mngr.parse_scoresheets_dat's docstring) -- load_event is
called once per round, and load_comp_event's upsert-by-raw_title means
every round after the first finds (not recreates) the same CompEvent row.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from dsr.load.wdsf import guess_style, load_marks, load_ranking_page  # noqa: F401 (re-exported)
from dsr.models import CompEvent, Competition
from dsr.parse.comp_mngr import SOURCE, CompMngrEventData


def load_comp_event(session: Session, competition: Competition, event: CompMngrEventData) -> CompEvent:
    existing = session.scalar(
        select(CompEvent).where(CompEvent.competition_id == competition.id, CompEvent.raw_title == event.raw_title)
    )
    if existing is not None:
        return existing
    comp_event = CompEvent(
        competition_id=competition.id,
        raw_title=event.raw_title,
        style=guess_style(event.raw_title),
    )
    session.add(comp_event)
    session.flush()
    return comp_event


def load_event(
    session: Session,
    competition: Competition,
    event: CompMngrEventData,
    judge_letter_to_person_id: dict[str, int],
) -> CompEvent:
    """Load one round's worth of a parsed CompMngrEventData: comp_event
    (upserted by raw_title) + this round's entries/results + marks."""
    comp_event = load_comp_event(session, competition, event)
    entry_by_competitor_no = load_ranking_page(session, SOURCE, competition, comp_event, event.ranking)
    load_marks(session, comp_event, entry_by_competitor_no, judge_letter_to_person_id, event.marks)
    return comp_event
