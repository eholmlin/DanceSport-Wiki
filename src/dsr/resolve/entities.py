"""Entity resolution: the only stage allowed to create person/partnership/organization rows.

M1-level per spec section 5, step order: external-ID match first (confidence 1.0,
auto), then exact person_alias hit (auto), else create a new person. The fuzzy
scoring/blocking steps (Jaro-Winkler, soundex blocking, resolution_queue
thresholds) are M2 work and deliberately not implemented here.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from dsr.models import Partnership, Person, PersonAlias
from dsr.parse.staging import StagingPersonRef

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
) -> Person:
    """Resolve a StagingPersonRef to a person row, creating one if needed.

    Step 1: external_ref (the source's stable per-athlete/official id) match,
    confidence 1.0, auto -- spec: "Where present, it solves entity resolution
    outright. Store it and trust it." We store this WDSF site-internal GUID in
    the same wdsf_min column the spec reserves for the WDSF member id number;
    it serves the identical purpose (a stable, unique, site-issued id) even
    though it isn't literally the MIN (see docs/wdsf-format-notes.md).

    Step 2: exact person_alias hit for this (raw_name, source), auto.

    Else: create a new person.
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

    if person is None:
        person = Person(
            display_name=ref.name,
            country=country,
            is_adjudicator=is_adjudicator,
            wdsf_min=ref.external_ref,
        )
        session.add(person)
        session.flush()
        _get_or_create_alias(
            session,
            person=person,
            raw_name=ref.name,
            source=source,
            confidence=1.0,
            resolved_by=RESOLVED_BY_EXTERNAL_ID if ref.external_ref else RESOLVED_BY_AUTO,
        )
        return person

    # Found an existing person via external_ref or alias -- backfill the
    # external ref if this pass has one and the stored row doesn't yet, and
    # make sure this exact (name, source) is on record in the alias ledger.
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


def resolve_partnership(session: Session, *, leader: Person, follower: Person, kind: str = "amateur") -> Partnership:
    partnership = session.scalar(
        select(Partnership).where(
            Partnership.leader_id == leader.id,
            Partnership.follower_id == follower.id,
            Partnership.kind == kind,
        )
    )
    if partnership is None:
        partnership = Partnership(leader_id=leader.id, follower_id=follower.id, kind=kind)
        session.add(partnership)
        session.flush()
    return partnership
