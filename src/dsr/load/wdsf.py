"""Load stage: idempotent upserts of parsed+resolved WDSF data into core tables.

Natural keys used for idempotency, where the spec's DDL doesn't define one:
- competition: UNIQUE(source, source_code) -- defined in schema.
- comp_event: UNIQUE(competition_id, raw_title) -- defined in schema.
- round: no DB constraint; (comp_event_id, round_type) used here as the key --
  not round_order, because round_order is a derived sequence position and
  some rounds (e.g. a "Redance" tie-break) only appear on the Marks page with
  no corresponding Ranking-page section to derive an order from. Such rounds
  are created on the fly by load_marks with a round_order appended after
  whatever the Ranking page did establish (see _get_or_create_round_by_label).
- entry: no DB constraint; (competition_id, partnership_id) used here as the
  key. Known limitation (see docs/wdsf-format-notes.md): a partnership entered
  in more than one comp_event at the same competition shares one entry row, so
  competitor_no reflects whichever comp_event was loaded most recently.
- result: PRIMARY KEY(comp_event_id, entry_id) -- defined in schema.
- mark: UNIQUE(round_id, entry_id, judge_person_id, dance) -- defined in schema.

Solo comp_events (no partner) use a degenerate partnership with kind='solo'
and follower_id=NULL, since the spec's schema models the partnership as the
competing unit and has no other slot for a lone competitor. 'solo' isn't
listed among the spec's example kind values (amateur|pro_am|professional|
formation) but nothing constrains the column to that list, and this keeps
solo results queryable through the same partnership/entry/result path.
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
        # Refresh mutable fields on every call, not just at creation -- in
        # particular source_updated_at, which an incremental refresh (see
        # scripts/refresh_ndca.py) needs to reflect the source's *current*
        # publish timestamp so it can detect the next revision too.
        existing.name = staging.name
        existing.start_date = staging.start_date
        existing.end_date = staging.end_date
        existing.city = staging.city
        existing.country = staging.country
        existing.url = staging.url
        if staging.source_updated_at is not None:
            existing.source_updated_at = staging.source_updated_at
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
        source_updated_at=staging.source_updated_at,
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
        select(Round).where(Round.comp_event_id == comp_event.id, Round.round_type == staging_round.round_type)
    )
    if existing is not None:
        existing.entries_in = staging_round.entries_in
        existing.recalled_count = staging_round.recalled_count
        existing.round_order = staging_round.round_order
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


def _get_or_create_round_by_label(session: Session, comp_event: CompEvent, round_label: str) -> Round:
    """Used by load_marks for round labels the Ranking page never listed as
    its own section (e.g. a tie-break "Redance"). Its true chronological slot
    among the numbered rounds isn't recoverable from what the page tells us,
    so this is a best-effort position -- but "final has the highest
    round_order" is preserved deliberately, since spec's rating weighting
    depends on final outranking every prelim round (section 5: "final > semi
    > prelim")."""
    existing = session.scalar(
        select(Round).where(Round.comp_event_id == comp_event.id, Round.round_type == round_label)
    )
    if existing is not None:
        return existing

    final_round = session.scalar(
        select(Round).where(Round.comp_event_id == comp_event.id, Round.round_type == "final")
    )
    if final_round is not None:
        new_order = final_round.round_order
        final_round.round_order += 1
    else:
        max_order = session.scalar(
            select(Round.round_order).where(Round.comp_event_id == comp_event.id).order_by(Round.round_order.desc())
        )
        new_order = (max_order or 0) + 1

    round_row = Round(
        comp_event_id=comp_event.id,
        round_type=round_label,
        round_order=new_order,
        entries_in=None,
        recalled_count=None,
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

    Solo entries (page.is_solo) load via a kind='solo' partnership -- see module docstring.
    """
    for staging_round in page.rounds:
        _load_round(session, comp_event, staging_round)

    entry_by_competitor_no: dict[str, Entry] = {}
    results_by_competitor_no = {r.competitor_no: r for r in page.results}

    for staging_entry in page.entries:
        if not staging_entry.competitor_no:
            continue  # excused entry: no result to attach, not a competing entry this round
        leader = resolve_person(session, source=source, ref=staging_entry.partner_1, country=staging_entry.country)
        if staging_entry.partner_2 is not None:
            follower = resolve_person(
                session,
                source=source,
                ref=staging_entry.partner_2,
                country=staging_entry.country,
                partner_person_ids=frozenset({leader.id}),
            )
            partnership = resolve_partnership(session, leader=leader, follower=follower, kind="amateur")
        else:
            partnership = resolve_partnership(session, leader=leader, follower=None, kind="solo")
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
) -> int:
    """Load StagingMark rows (from either the recall Marks page or the Final page).

    Rounds are resolved by label (round_label), auto-creating one if this
    label had no corresponding Ranking-page section -- see
    _get_or_create_round_by_label.

    Existing marks are batch-fetched once per call (one query per involved
    round, via the round_id IN (...) which the UNIQUE(round_id, entry_id,
    judge_person_id, dance) constraint indexes) rather than one SELECT per
    mark. At ~3M total marks, one-query-per-mark was the dominant cost of the
    whole load/reprocess pipeline -- an event with 11 judges x 5 dances is 55
    marks, i.e. 55 round-trips that are now 1.
    """
    rounds_by_label = {
        r.round_type: r for r in session.scalars(select(Round).where(Round.comp_event_id == comp_event.id))
    }
    for label in {m.round_label for m in marks} - rounds_by_label.keys():
        rounds_by_label[label] = _get_or_create_round_by_label(session, comp_event, label)

    round_ids = [r.id for r in rounds_by_label.values()]
    existing_by_key: dict[tuple, Mark] = {}
    if round_ids:
        for existing_mark in session.scalars(select(Mark).where(Mark.round_id.in_(round_ids))):
            existing_by_key[(existing_mark.round_id, existing_mark.entry_id, existing_mark.judge_person_id, existing_mark.dance)] = (
                existing_mark
            )

    loaded = 0
    for staging_mark in marks:
        round_row = rounds_by_label[staging_mark.round_label]
        entry = entry_by_competitor_no.get(staging_mark.competitor_no)
        judge_person_id = judge_letter_to_person_id.get(staging_mark.judge_letter)
        if entry is None or judge_person_id is None:
            raise ValueError(
                f"mark references unknown entry/judge: round_label={staging_mark.round_label!r} "
                f"competitor_no={staging_mark.competitor_no!r} judge_letter={staging_mark.judge_letter!r}"
            )

        key = (round_row.id, entry.id, judge_person_id, staging_mark.dance)
        existing = existing_by_key.get(key)
        if existing is not None:
            existing.recalled = staging_mark.recalled
            existing.placement = staging_mark.placement
            continue
        new_mark = Mark(
            round_id=round_row.id,
            entry_id=entry.id,
            judge_person_id=judge_person_id,
            dance=staging_mark.dance,
            recalled=staging_mark.recalled,
            placement=staging_mark.placement,
        )
        session.add(new_mark)
        existing_by_key[key] = new_mark  # dedupe correctly against dupes within this same call too
        loaded += 1
    session.flush()
    return loaded
