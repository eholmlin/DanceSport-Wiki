"""Load stage: idempotent upserts of parsed+resolved WDSF data into core tables.

Natural keys used for idempotency, where the spec's DDL doesn't define one:
- competition: UNIQUE(source, source_code) -- defined in schema.
- comp_event: UNIQUE(competition_id, raw_title) -- defined in schema.
- round: no DB constraint; (comp_event_id, round_order) used here as the key.
- entry: no DB constraint; (competition_id, partnership_id) used here as the
  key. Known limitation (see docs/wdsf-format-notes.md): a partnership entered
  in more than one comp_event at the same competition shares one entry row, so
  competitor_no reflects whichever comp_event was loaded most recently.
- result: PRIMARY KEY(comp_event_id, entry_id) -- defined in schema.
- mark: UNIQUE(round_id, entry_id, judge_person_id, dance) -- defined in schema.

Solo comp_events (no partner) are skipped: the spec's schema models the
partnership as the competing unit with entry.partnership_id required, and has
no representation for a lone competitor. Flagged as a follow-up rather than
worked around unilaterally.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from dsr.models import CompEvent, Competition, Entry, Mark, Round, Result
from dsr.parse.staging import (
    RankingPage,
    StagingCompEventRef,
    StagingCompetition,
    StagingMark,
    StagingOfficial,
    StagingPersonRef,
)
from dsr.resolve.entities import resolve_partnership, resolve_person

_STYLE_KEYWORDS = {
    "Latin": "Latin",
    "Standard": "Standard",
    "Smooth": "Smooth",
    "Rhythm": "Rhythm",
}


def guess_style(raw_title: str) -> str | None:
    """Best-effort keyword guess only -- raw_title is the source of truth (spec 4)."""
    for keyword, style in _STYLE_KEYWORDS.items():
        if keyword in raw_title:
            return style
    return None


def load_competition(session: Session, staging: StagingCompetition) -> Competition:
    existing = session.scalar(
        select(Competition).where(Competition.source == staging.source, Competition.source_code == staging.source_code)
    )
    if existing is not None:
        return existing
    competition = Competition(
        source=staging.source,
        source_code=staging.source_code,
        name=staging.name,
        start_date=staging.start_date,
        end_date=staging.end_date,
        city=staging.city,
        country=staging.country,
        sanctioning_body=staging.sanctioning_body,
        url=staging.url,
    )
    session.add(competition)
    session.flush()
    return competition


def load_comp_event(session: Session, competition: Competition, ref: StagingCompEventRef) -> CompEvent:
    existing = session.scalar(
        select(CompEvent).where(CompEvent.competition_id == competition.id, CompEvent.raw_title == ref.raw_title)
    )
    if existing is not None:
        return existing
    comp_event = CompEvent(
        competition_id=competition.id,
        raw_title=ref.raw_title,
        style=guess_style(ref.raw_title),
    )
    session.add(comp_event)
    session.flush()
    return comp_event


def _load_round(session: Session, comp_event: CompEvent, staging_round) -> Round:
    existing = session.scalar(
        select(Round).where(Round.comp_event_id == comp_event.id, Round.round_order == staging_round.round_order)
    )
    if existing is not None:
        existing.entries_in = staging_round.entries_in
        existing.recalled_count = staging_round.recalled_count
        existing.round_type = staging_round.round_type
        return existing
    round_row = Round(
        comp_event_id=comp_event.id,
        round_type=staging_round.round_type,
        round_order=staging_round.round_order,
        entries_in=staging_round.entries_in,
        recalled_count=staging_round.recalled_count,
    )
    session.add(round_row)
    session.flush()
    return round_row


def _load_entry(session: Session, competition: Competition, partnership_id: int, competitor_no: str) -> Entry:
    existing = session.scalar(
        select(Entry).where(Entry.competition_id == competition.id, Entry.partnership_id == partnership_id)
    )
    if existing is not None:
        existing.competitor_no = competitor_no
        return existing
    entry = Entry(competition_id=competition.id, partnership_id=partnership_id, competitor_no=competitor_no)
    session.add(entry)
    session.flush()
    return entry


def _load_result(session: Session, comp_event: CompEvent, entry: Entry, staging_result) -> Result:
    existing = session.get(Result, {"comp_event_id": comp_event.id, "entry_id": entry.id})
    if existing is not None:
        existing.placement_low = staging_result.placement_low
        existing.placement_high = staging_result.placement_high
        existing.made_final = staging_result.made_final
        existing.field_size = staging_result.field_size
        return existing
    result = Result(
        comp_event_id=comp_event.id,
        entry_id=entry.id,
        placement_low=staging_result.placement_low,
        placement_high=staging_result.placement_high,
        made_final=staging_result.made_final,
        field_size=staging_result.field_size,
    )
    session.add(result)
    session.flush()
    return result


def load_ranking_page(
    session: Session, source: str, competition: Competition, comp_event: CompEvent, page: RankingPage
) -> dict[str, Entry]:
    """Load rounds + entries + results. Returns competitor_no -> Entry for mark loading.

    Skips solo entries (page.is_solo) -- see module docstring.
    """
    if page.is_solo:
        return {}

    for staging_round in page.rounds:
        _load_round(session, comp_event, staging_round)

    entry_by_competitor_no: dict[str, Entry] = {}
    results_by_competitor_no = {r.competitor_no: r for r in page.results}

    for staging_entry in page.entries:
        if not staging_entry.competitor_no:
            continue  # excused couple: no result to attach, not a competing entry this round
        leader = resolve_person(session, source=source, ref=staging_entry.partner_1, country=staging_entry.country)
        follower = resolve_person(session, source=source, ref=staging_entry.partner_2, country=staging_entry.country)
        partnership = resolve_partnership(session, leader=leader, follower=follower, kind="amateur")
        entry = _load_entry(session, competition, partnership.id, staging_entry.competitor_no)
        entry_by_competitor_no[staging_entry.competitor_no] = entry

        staging_result = results_by_competitor_no.get(staging_entry.competitor_no)
        if staging_result is not None:
            _load_result(session, comp_event, entry, staging_result)

    return entry_by_competitor_no


def load_officials(session: Session, source: str, officials: list[StagingOfficial]) -> dict[str, int]:
    """Resolve each judge to a person row. Returns judge_letter -> person_id."""
    letter_to_person_id: dict[str, int] = {}
    for official in officials:
        person = resolve_person(
            session,
            source=source,
            ref=StagingPersonRef(name=official.name, external_ref=official.external_ref),
            country=official.country,
            is_adjudicator=True,
        )
        letter_to_person_id[official.letter] = person.id
    return letter_to_person_id


def load_marks(
    session: Session,
    comp_event: CompEvent,
    entry_by_competitor_no: dict[str, Entry],
    judge_letter_to_person_id: dict[str, int],
    marks: list[StagingMark],
    *,
    final_round_order: int | None,
) -> int:
    """Load StagingMark rows (from either the recall Marks page or the Final page).

    marks with round_order=None (the Final page's combined placement) are
    attached to `final_round_order`, the round_order already assigned to that
    comp_event's "final" StagingRound during ranking-page load.
    """
    rounds_by_order = {
        r.round_order: r for r in session.scalars(select(Round).where(Round.comp_event_id == comp_event.id))
    }

    loaded = 0
    for staging_mark in marks:
        round_order = staging_mark.round_order if staging_mark.round_order is not None else final_round_order
        round_row = rounds_by_order.get(round_order)
        entry = entry_by_competitor_no.get(staging_mark.competitor_no)
        judge_person_id = judge_letter_to_person_id.get(staging_mark.judge_letter)
        if round_row is None or entry is None or judge_person_id is None:
            raise ValueError(
                f"mark references unknown round/entry/judge: round_order={round_order!r} "
                f"competitor_no={staging_mark.competitor_no!r} judge_letter={staging_mark.judge_letter!r}"
            )

        existing = session.scalar(
            select(Mark).where(
                Mark.round_id == round_row.id,
                Mark.entry_id == entry.id,
                Mark.judge_person_id == judge_person_id,
                Mark.dance.is_(None) if staging_mark.dance is None else Mark.dance == staging_mark.dance,
            )
        )
        if existing is not None:
            existing.recalled = staging_mark.recalled
            existing.placement = staging_mark.placement
            continue
        session.add(
            Mark(
                round_id=round_row.id,
                entry_id=entry.id,
                judge_person_id=judge_person_id,
                dance=staging_mark.dance,
                recalled=staging_mark.recalled,
                placement=staging_mark.placement,
            )
        )
        loaded += 1
    session.flush()
    return loaded
