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
    every UPDATE targets rows that no longer exist)."""
    if keep_person_id == remove_person_id:
        return
    if session.get(Person, remove_person_id) is None:
        return

    session.execute(
        update(PersonAlias).where(PersonAlias.person_id == remove_person_id).values(person_id=keep_person_id)
    )
    session.execute(
        update(Partnership).where(Partnership.leader_id == remove_person_id).values(leader_id=keep_person_id)
    )
    session.execute(
        update(Partnership).where(Partnership.follower_id == remove_person_id).values(follower_id=keep_person_id)
    )
    session.execute(
        update(Partnership).where(Partnership.student_id == remove_person_id).values(student_id=keep_person_id)
    )
    session.execute(
        update(Affiliation).where(Affiliation.person_id == remove_person_id).values(person_id=keep_person_id)
    )
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
