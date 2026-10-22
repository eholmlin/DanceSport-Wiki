"""Load stage glue for Cre8tive Score.

The rounds/entries/results/marks upserts and entity resolution are the
same ones WDSF, NDCA and Comp Manager use (dsr.load.wdsf), reused as-is.
Like Comp Manager, a competition's judges are resolved once up front
(load_officials) and the id -> person map threaded into every event.
"""
from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from dsr.load.wdsf import guess_style, load_marks, load_ranking_page  # noqa: F401 (re-exported)
from dsr.models import CompEvent, Competition, MarkHesitation, Round
from dsr.models import Entry  # noqa: F401 (type reference in load_hesitations)
from dsr.parse.cre8tive import SOURCE, Cre8tiveEvent


def load_comp_event(session: Session, competition: Competition, event: Cre8tiveEvent) -> CompEvent:
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


def load_hesitations(
    session: Session,
    comp_event: CompEvent,
    entry_by_competitor_no: dict[str, "Entry"],
    judge_id_to_person_id: dict[str, int],
    hesitations: list,
) -> int:
    """Replace this event's hesitation rows with the parsed set (a re-run
    after the source changed a flag must not leave the old one behind)."""
    rounds = {r.round_type: r for r in session.scalars(select(Round).where(Round.comp_event_id == comp_event.id))}
    if rounds:
        session.execute(delete(MarkHesitation).where(MarkHesitation.round_id.in_([r.id for r in rounds.values()])))
    seen: set[tuple] = set()
    for h in hesitations:
        round_row = rounds.get(h.round_label)
        entry = entry_by_competitor_no.get(h.competitor_no)
        judge_person_id = judge_id_to_person_id.get(h.judge_letter)
        if round_row is None or entry is None or judge_person_id is None:
            continue
        key = (round_row.id, entry.id, judge_person_id, h.dance)
        if key in seen:
            continue
        seen.add(key)
        session.add(
            MarkHesitation(round_id=round_row.id, entry_id=entry.id, judge_person_id=judge_person_id, dance=h.dance)
        )
    session.flush()
    return len(seen)


def load_event(
    session: Session,
    competition: Competition,
    event: Cre8tiveEvent,
    judge_id_to_person_id: dict[str, int],
) -> CompEvent:
    """Load one sku: comp_event (upserted by raw_title) + all its rounds,
    entries, results, marks and hesitation flags."""
    comp_event = load_comp_event(session, competition, event)
    entry_by_competitor_no = load_ranking_page(session, SOURCE, competition, comp_event, event.ranking)
    load_marks(session, comp_event, entry_by_competitor_no, judge_id_to_person_id, event.marks)
    load_hesitations(session, comp_event, entry_by_competitor_no, judge_id_to_person_id, event.hesitations)
    return comp_event
