"""Working the resolution_queue: human decisions on uncertain fuzzy matches.

Every queued row was created because resolve_person() found a plausible but
not-confident-enough candidate (score 0.75-0.93) and, per spec section 2
("never silently merge"), created a *new* person for the raw_name rather than
guessing. `context.new_person_id` is that new (possibly-duplicate) person.
A human decision here either merges it into the suggested candidate or
confirms it's genuinely a different person.
"""
from __future__ import annotations

import datetime as dt
from typing import Literal

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from dsr.models import Affiliation, Entry, Mark, Partnership, Person, PersonAlias, ResolutionQueue

Decision = Literal["merge", "new_person", "rejected"]


def list_pending(session: Session) -> list[ResolutionQueue]:
    return list(session.scalars(select(ResolutionQueue).where(ResolutionQueue.status == "pending")).all())


def merge_people(session: Session, *, keep_person_id: int, remove_person_id: int) -> None:
    """Reassign every FK pointing at remove_person_id over to keep_person_id,
    then delete the now-orphaned person row. Idempotent to call twice (second
    call finds remove_person_id already gone and simply does nothing, since
    every UPDATE targets rows that no longer exist).

    PersonAlias, Partnership, and Affiliation each carry a uniqueness
    constraint that a blind bulk UPDATE can violate whenever keep_person_id
    and remove_person_id independently already have a row for the same
    (raw_name, source) / (leader, follower, kind) / organization -- e.g. both
    danced with the same third partner under the same partnership kind. Real
    case: merging two spelling variants of the same student hit
    "UNIQUE constraint failed: partnership.leader_id, partnership.follower_id,
    partnership.kind" because both variants had a partnership with the same
    instructor. Handled by redirecting onto the row that already exists
    (moving Partnership's Entry rows along with it) and dropping the
    now-redundant duplicate, instead of updating it in place.
    """
    if keep_person_id == remove_person_id:
        return
    if session.get(Person, remove_person_id) is None:
        return

    for alias in session.scalars(select(PersonAlias).where(PersonAlias.person_id == remove_person_id)).all():
        existing = session.scalar(
            select(PersonAlias).where(
                PersonAlias.raw_name == alias.raw_name,
                PersonAlias.source == alias.source,
                PersonAlias.person_id == keep_person_id,
            )
        )
        if existing is not None:
            session.delete(alias)
        else:
            alias.person_id = keep_person_id
    session.flush()

    for role, other_role in (("leader_id", "follower_id"), ("follower_id", "leader_id")):
        role_col = getattr(Partnership, role)
        other_col = getattr(Partnership, other_role)
        for partnership in session.scalars(select(Partnership).where(role_col == remove_person_id)).all():
            other_value = getattr(partnership, other_role)
            existing = session.scalar(
                select(Partnership).where(
                    getattr(Partnership, role) == keep_person_id,
                    other_col == other_value,
                    Partnership.kind == partnership.kind,
                    Partnership.id != partnership.id,
                )
            )
            if existing is not None:
                session.execute(
                    update(Entry).where(Entry.partnership_id == partnership.id).values(partnership_id=existing.id)
                )
                session.delete(partnership)
            else:
                setattr(partnership, role, keep_person_id)
    session.flush()

    session.execute(
        update(Partnership).where(Partnership.student_id == remove_person_id).values(student_id=keep_person_id)
    )

    for affiliation in session.scalars(select(Affiliation).where(Affiliation.person_id == remove_person_id)).all():
        existing = session.get(Affiliation, (keep_person_id, affiliation.organization_id))
        if existing is not None:
            session.delete(affiliation)
        else:
            affiliation.person_id = keep_person_id
    session.flush()

    session.execute(update(Mark).where(Mark.judge_person_id == remove_person_id).values(judge_person_id=keep_person_id))
    session.execute(
        update(ResolutionQueue)
        .where(ResolutionQueue.candidate_person_id == remove_person_id)
        .values(candidate_person_id=keep_person_id)
    )

    remove_person = session.get(Person, remove_person_id)
    session.delete(remove_person)
    session.flush()


def decide(session: Session, queue_id: int, decision: Decision) -> ResolutionQueue:
    entry = session.get(ResolutionQueue, queue_id)
    if entry is None:
        raise ValueError(f"no resolution_queue row with id={queue_id}")
    if entry.status != "pending":
        raise ValueError(f"resolution_queue row {queue_id} already decided (status={entry.status!r})")

    if decision == "merge":
        new_person_id = (entry.context or {}).get("new_person_id")
        if new_person_id is None:
            raise ValueError(f"resolution_queue row {queue_id} has no context.new_person_id to merge from")
        merge_people(session, keep_person_id=entry.candidate_person_id, remove_person_id=new_person_id)

    entry.status = "merged" if decision == "merge" else decision
    entry.decided_at = dt.datetime.now(dt.timezone.utc)
    session.flush()
    return entry
