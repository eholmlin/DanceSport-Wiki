"""Load stage glue for Comp Manager heat lists -- see
dsr.parse.comp_mngr_heatlist for the format and what it can't tell us.

Comp Manager has no calendar feed (see scripts/bulk_load_comp_mngr.py), so
the events whose heat lists are refreshed live in HEATLIST_EVENTS below,
added by hand with the dates the heat list page itself doesn't carry.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from dsr.fetch.client import PoliteFetcher
from dsr.fetch.storage import save_raw_document
from dsr.load.heatlist import load_scheduled_heat
from dsr.load.wdsf import load_competition
from dsr.parse.comp_mngr_heatlist import SOURCE, parse_heatlists
from dsr.parse.staging import StagingCompetition

BASE = "http://www.comp-mngr.com"
# Heat-list names are NDCA-style "Firstname Lastname" (after flipping), for
# NDCA-sanctioned events, so people are matched in NDCA's alias namespace.
NAME_SOURCE = "ndca_premier"


@dataclass(frozen=True)
class CompMngrHeatlistEvent:
    slug: str  # the event's directory on comp-mngr.com, e.g. "ohiostar2026"
    name: str
    start_date: dt.date
    end_date: dt.date
    city: str
    time_zone: str  # the venue's, so "already danced" is judged on the venue's clock


HEATLIST_EVENTS = [
    CompMngrHeatlistEvent(
        slug="ohiostar2026",
        name="Ohio Star Ball",
        start_date=dt.date(2026, 11, 17),
        end_date=dt.date(2026, 11, 22),
        city="Columbus",
        time_zone="America/New_York",
    ),
]


def heatlist_url(event: CompMngrHeatlistEvent) -> str:
    return f"{BASE}/{event.slug}/{event.slug}_HeatLists.htm"


def events_to_refresh(today: dt.date, events=HEATLIST_EVENTS) -> list[CompMngrHeatlistEvent]:
    """Events still to come or in progress (a finished one's heat list is
    superseded by its results; see drop_finished_competitions_with_results)
    and starting within the next 30 days."""
    return [e for e in events if e.end_date >= today and e.start_date <= today + dt.timedelta(days=30)]


def events_with_results_due(today: dt.date, events=HEATLIST_EVENTS, *, days_after: int = 7) -> list[CompMngrHeatlistEvent]:
    """Events that have started and finished no more than `days_after` days
    ago -- when results are being posted (see scripts/bulk_load_comp_mngr.py,
    refresh_live_events)."""
    return [e for e in events if e.start_date <= today <= e.end_date + dt.timedelta(days=days_after)]


def load_comp_mngr_heatlist(
    session: Session, fetcher: PoliteFetcher, event: CompMngrHeatlistEvent, *, now_utc: dt.datetime | None = None
) -> int:
    """Fetches and loads one event's heat list. Returns the number of
    scheduled_heat rows loaded/updated (0 if the page isn't up yet)."""
    url = heatlist_url(event)
    resp = fetcher.get(url)
    if resp.status_code != 200:
        return 0
    save_raw_document(session, source=SOURCE, url=url, content=resp.content, http_status=resp.status_code)
    now_local = (now_utc or dt.datetime.now(dt.timezone.utc)).astimezone(ZoneInfo(event.time_zone)).replace(tzinfo=None)
    staged = parse_heatlists(resp.content, start_date=event.start_date, now_local=now_local)
    competition = load_competition(
        session,
        StagingCompetition(
            source=SOURCE,
            source_code=event.slug,
            name=event.name,
            start_date=event.start_date,
            end_date=event.end_date,
            city=event.city,
            country="USA",
            sanctioning_body="NDCA",
            url=url,
        ),
        advance_source_updated_at=False,
    )
    for i, heat in enumerate(staged, start=1):
        load_scheduled_heat(session, SOURCE, competition, heat, resolve_source=NAME_SOURCE)
        if i % 500 == 0:
            session.commit()
    session.commit()
    return len(staged)
