"""Load stage glue for heat lists (pre-competition schedules) -- see
dsr.models.ScheduledHeat for why this is kept separate from the
results-oriented load/wdsf.py path, and dsr.parse.ndca.parse_heatlist_attendee
for the source format. NDCA Premier only for now.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from dsr.models import Competition, Partnership, ScheduledHeat
from dsr.parse.staging import StagingScheduledHeat
from dsr.resolve.entities import resolve_partnership, resolve_person


def _resolve_partnership_either_order(session: Session, person_a, person_b, *, kind: str) -> Partnership:
    """Unlike results (see dsr.parse.ndca.parse_heatlist_attendee's
    docstring), a heat-list couple's leader/follower order depends on
    which attendee happened to be queried, not a stable source-side
    order -- checking both orders before resolve_partnership's own
    create-or-find avoids creating a second, duplicate Partnership row
    for a couple that's already on file (e.g. from a past competition's
    results) in the opposite order."""
    existing = session.scalar(
        select(Partnership).where(
            or_(
                and_(Partnership.leader_id == person_a.id, Partnership.follower_id == person_b.id),
                and_(Partnership.leader_id == person_b.id, Partnership.follower_id == person_a.id),
            ),
            Partnership.kind == kind,
        )
    )
    if existing is not None:
        return existing
    return resolve_partnership(session, leader=person_a, follower=person_b, kind=kind)


def load_scheduled_heat(
    session: Session, source: str, competition: Competition, staging: StagingScheduledHeat
) -> ScheduledHeat:
    """Upsert one StagingScheduledHeat by its natural key (source,
    source_event_id, round_name, partnership_id) -- re-loading the same
    heat list is idempotent, and updates the row's fields (a schedule can
    shift between refreshes) plus updated_at rather than skipping."""
    partner_1 = resolve_person(session, source=source, ref=staging.partner_1, country=None)
    if staging.partner_2 is not None:
        partner_2 = resolve_person(
            session, source=source, ref=staging.partner_2, country=None, partner_person_ids=frozenset({partner_1.id})
        )
        partnership = _resolve_partnership_either_order(session, partner_1, partner_2, kind="amateur")
    else:
        partnership = resolve_partnership(session, leader=partner_1, follower=None, kind="solo")

    now = dt.datetime.now(dt.timezone.utc)
    existing = session.scalar(
        select(ScheduledHeat).where(
            ScheduledHeat.source == source,
            ScheduledHeat.source_event_id == staging.source_event_id,
            ScheduledHeat.round_name == staging.round_name,
            ScheduledHeat.partnership_id == partnership.id,
        )
    )
    if existing is not None:
        existing.competition_id = competition.id
        existing.event_name = staging.event_name
        existing.heat_number = staging.heat_number
        existing.session = staging.session
        existing.floor = staging.floor
        existing.competitor_no = staging.competitor_no
        existing.scheduled_time = staging.scheduled_time
        existing.is_complete = staging.is_complete
        existing.updated_at = now
        return existing

    row = ScheduledHeat(
        competition_id=competition.id,
        partnership_id=partnership.id,
        source=source,
        source_event_id=staging.source_event_id,
        event_name=staging.event_name,
        round_name=staging.round_name,
        heat_number=staging.heat_number,
        session=staging.session,
        floor=staging.floor,
        competitor_no=staging.competitor_no,
        scheduled_time=staging.scheduled_time,
        is_complete=staging.is_complete,
        updated_at=now,
    )
    session.add(row)
    session.flush()
    return row
