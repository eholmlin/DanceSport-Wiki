"""Load stage glue for DanceComp (dancecomp.io).

The heavy lifting (rounds/entries/results upserts, entity resolution) is
identical to WDSF's/NDCA's/Comp Manager's and lives in dsr.load.wdsf --
reused directly here. Only comp_event creation needed a thin
DanceComp-specific version, same reasoning as dsr.load.comp_mngr.

Unlike Comp Manager, this source has no per-judge marks at all (see
dsr.parse.dancecomp module docstring) -- load_marks/load_officials are
never called here, there is nothing to pass them.

Like Comp Manager, one event's separate rounds (recall + final) arrive as
separate DancecompEventData sharing one raw_title -- load_event is called
once per round, and load_comp_event's upsert-by-raw_title means every
round after the first finds (not recreates) the same CompEvent row.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from dsr.load.wdsf import guess_style, load_ranking_page  # noqa: F401 (re-exported)
from dsr.models import CompEvent, Competition
from dsr.parse.dancecomp import SOURCE, DancecompEventData


def load_comp_event(session: Session, competition: Competition, event: DancecompEventData) -> CompEvent:
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


def load_event(session: Session, competition: Competition, event: DancecompEventData) -> CompEvent:
    """Load one round's worth of a parsed DancecompEventData: comp_event
    (upserted by raw_title) + this round's entries/results. No marks."""
    comp_event = load_comp_event(session, competition, event)
    load_ranking_page(session, SOURCE, competition, comp_event, event.ranking)
    return comp_event
