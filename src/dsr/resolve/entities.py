"""Entity resolution: the only stage allowed to create person/partnership/organization rows.

Implements spec section 5's algorithm in order:
1. External-ID match (the source's stable per-athlete/official id) -> auto, confidence 1.0.
2. Exact person_alias hit for this (raw_name, source) -> auto, confidence 1.0.
3-5. Blocking + Jaro-Winkler scoring (dsr.resolve.fuzzy) -> auto-merge at
   score >= 0.93, resolution_queue entry for 0.75-0.93, else a new person.
   "Never silently merge two people" (spec section 2): steps 3-5 never reuse
   an existing person outside the auto-merge threshold -- an uncertain match
   always gets its own new person row plus a queue entry for human review,
   rather than merging speculatively.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from dsr.models import Partnership, Person, PersonAlias, ResolutionQueue
from dsr.parse.staging import StagingPersonRef
from dsr.resolve.fuzzy import AUTO_MERGE_THRESHOLD, QUEUE_THRESHOLD, find_candidates

RESOLVED_BY_EXTERNAL_ID = "external_id"
RESOLVED_BY_AUTO = "auto"


def _get_or_create_alias(
    session: Session, *, person: Person, raw_name: str, source: str, confidence: float, resolved_by: str
) -> None:
    existing = session.scalar(
        select(PersonAlias).where(
            PersonAlias.raw_name == raw_name,
            PersonAlias.source == source,
            PersonAlias.person_id == person.id,
        )
    )
    if existing is None:
        session.add(
            PersonAlias(
                person_id=person.id,
                raw_name=raw_name,
                source=source,
                confidence=confidence,
                resolved_by=resolved_by,
            )
        )
        session.flush()


def resolve_person(
    session: Session,
    *,
    source: str,
    ref: StagingPersonRef,
    country: str | None = None,
    is_adjudicator: bool = False,
    partner_person_ids: frozenset[int] = frozenset(),
) -> Person:
    """Resolve a StagingPersonRef to a person row, creating one if needed.

    `partner_person_ids` (already-resolved partner(s) for this same entry, if
    any) feeds the fuzzy path's shared-partner-history signal -- pass the
    leader's resolved id when resolving the follower, for example.
    """
    person: Person | None = None

    if ref.external_ref:
        person = session.scalar(select(Person).where(Person.wdsf_min == ref.external_ref))

    if person is None:
        alias = session.scalar(
            select(PersonAlias).where(PersonAlias.raw_name == ref.name, PersonAlias.source == source)
        )
        if alias is not None:
            person = session.get(Person, alias.person_id)

    if person is not None:
        # Found via external_ref or exact alias -- backfill the external ref
        # if this pass has one and the stored row doesn't yet, and make sure
        # this exact (name, source) is on record in the alias ledger.
        if ref.external_ref and not person.wdsf_min:
            person.wdsf_min = ref.external_ref
        _get_or_create_alias(
            session,
            person=person,
            raw_name=ref.name,
            source=source,
            confidence=1.0,
            resolved_by=RESOLVED_BY_EXTERNAL_ID if ref.external_ref else RESOLVED_BY_AUTO,
        )
        return person

    # Steps 3-5 (fuzzy blocking/scoring) only apply when there's no external_ref
    # at all. A *present-but-unmatched* external_ref isn't ambiguous -- it's a
    # source-issued id we've simply never seen before, and since it's unique
    # per athlete forever, that alone conclusively means this is a new person.
    # Skipping this check was a real bug: nearly every athlete's *first*
    # appearance in the data has an external_ref that (by definition) matches
    # no one yet, so without this guard almost every new person ran the fuzzy
    # gauntlet anyway and picked up spurious resolution_queue entries against
    # unrelated people who merely shared a surname or blocking key.
    candidates = (
        []
        if ref.external_ref
        else find_candidates(session, name=ref.name, source=source, country=country, partner_person_ids=partner_person_ids)
    )
    top = candidates[0] if candidates else None

    if top is not None and top.score >= AUTO_MERGE_THRESHOLD:
        person = top.person
        if ref.external_ref and not person.wdsf_min:
            person.wdsf_min = ref.external_ref
        _get_or_create_alias(
            session, person=person, raw_name=ref.name, source=source, confidence=top.score, resolved_by=RESOLVED_BY_AUTO
        )
        return person

    # No confident match: this raw_name gets its own new person, confidently
    # (confidence=1.0 for *this* mapping) -- but if there was a plausible-but-
    # uncertain candidate, flag it for human review rather than silently
    # deciding either way.
    person = Person(display_name=ref.name, country=country, is_adjudicator=is_adjudicator, wdsf_min=ref.external_ref)
    session.add(person)
    session.flush()
    _get_or_create_alias(
        session, person=person, raw_name=ref.name, source=source, confidence=1.0, resolved_by=RESOLVED_BY_AUTO
    )

    if top is not None and QUEUE_THRESHOLD <= top.score < AUTO_MERGE_THRESHOLD:
        session.add(
            ResolutionQueue(
                raw_name=ref.name,
                source=source,
                candidate_person_id=top.person.id,
                score=top.score,
                context={
                    "new_person_id": person.id,
                    "name_score": top.name_score,
                    "shared_partner": top.shared_partner,
                    "same_country": top.same_country,
                    "country": country,
                },
            )
        )
        session.flush()

    return person


def resolve_partnership(
    session: Session, *, leader: Person, follower: Person | None = None, kind: str = "amateur"
) -> Partnership:
    """Resolve/create a partnership. `follower=None` represents a solo competitor
    (kind='solo'); SQLAlchemy translates `follower_id == None` to `IS NULL`, so
    this stays idempotent the same way the couple case is."""
    follower_id = follower.id if follower is not None else None
    partnership = session.scalar(
        select(Partnership).where(
            Partnership.leader_id == leader.id,
            Partnership.follower_id == follower_id,
            Partnership.kind == kind,
        )
    )
    if partnership is None:
        partnership = Partnership(leader_id=leader.id, follower_id=follower_id, kind=kind)
        session.add(partnership)
        session.flush()
    return partnership
