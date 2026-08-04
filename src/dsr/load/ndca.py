"""Load stage glue for NDCA Premier.

The heavy lifting (rounds/entries/results/marks upserts, entity resolution)
is identical to WDSF's and lives in dsr.load.wdsf -- those functions are
already source-parameterized and operate on the same staging dataclasses, so
they're reused directly here rather than duplicated. Only competition/
comp_event creation needed a thin NDCA-specific version, since
StagingCompEventRef carries WDSF-specific page URLs that NDCA's single-blob
feed doesn't have.

Known simplification: load_ranking_page hardcodes partnership kind='amateur'
for every couple (or 'solo' for a lone competitor), regardless of source.
NDCA genuinely distinguishes Amateur/Pro-Am/Professional (the per-competitor
"Pro_Am_Status" field: "A"/"P") in a way WDSF's Adult-Latin-style couple
events mostly don't need to -- not modeled yet. Every NDCA partnership loads
as 'amateur' for now; revisit if Pro-Am tracking becomes a real requirement.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from dsr.load.wdsf import guess_style, load_marks, load_officials, load_ranking_page  # noqa: F401 (re-exported)
from dsr.models import CompEvent, Competition
from dsr.parse.ndca import NdcaEventData


def load_comp_event(session: Session, competition: Competition, event: NdcaEventData) -> CompEvent:
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


def load_event(session: Session, competition: Competition, event: NdcaEventData) -> CompEvent:
    """Load one fully-parsed NdcaEventData: comp_event + rounds/entries/results + officials + marks."""
    comp_event = load_comp_event(session, competition, event)
    entry_by_competitor_no = load_ranking_page(session, "ndca_premier", competition, comp_event, event.ranking)
    letter_to_person_id = load_officials(session, "ndca_premier", event.officials)
    load_marks(session, comp_event, entry_by_competitor_no, letter_to_person_id, event.marks)
    return comp_event
