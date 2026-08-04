"""M1-equivalent for NDCA Premier: load one real competition end to end from
the live site (every competitor's feed, since no event index exists -- see
docs/ndca-format-notes.md), then print the full result list for eyeballing.

Usage: python scripts/load_ndca_competition.py <cyi>
"""
from __future__ import annotations

import sys

from sqlalchemy import select

from dsr.db import get_session
from dsr.fetch.client import PoliteFetcher
from dsr.fetch.storage import save_raw_document
from dsr.load.ndca import load_event
from dsr.load.wdsf import load_competition
from dsr.models import CompEvent, Entry, Partnership, Person, Result
from dsr.parse.ndca import ParseError, parse_competition, parse_competitor_feed, parse_roster

BASE = "https://ndcapremier.com"


def main(cyi: str) -> None:
    session = get_session()
    fetcher = PoliteFetcher()

    meta_url = f"{BASE}/feed/compyears/?cyi={cyi}"
    meta_resp = fetcher.get(meta_url)
    save_raw_document(session, source="ndca_premier", url=meta_url, content=meta_resp.content, http_status=meta_resp.status_code)
    competition_staging = parse_competition(meta_resp.content)
    competition = load_competition(session, competition_staging)
    print(f"Competition: {competition.name} ({competition.start_date})")

    roster_url = f"{BASE}/feed/results/?cyi={cyi}"
    roster_resp = fetcher.get(roster_url)
    save_raw_document(session, source="ndca_premier", url=roster_url, content=roster_resp.content, http_status=roster_resp.status_code)
    roster = parse_roster(roster_resp.content)
    print(f"Roster: {len(roster)} competitors")

    loaded_event_ids: set[str] = set()
    n_events_loaded = 0
    for i, (competitor_id, name) in enumerate(roster):
        url = f"{BASE}/feed/results/?cyi={cyi}&id={competitor_id}"
        resp = fetcher.get(url)
        if resp.status_code != 200:
            continue
        save_raw_document(session, source="ndca_premier", url=url, content=resp.content, http_status=resp.status_code)
        try:
            events = parse_competitor_feed(resp.content)
        except ParseError as exc:
            print(f"  (parse error for {name}: {exc})")
            continue
        for event in events:
            if event.source_code in loaded_event_ids:
                continue
            load_event(session, competition, event)
            loaded_event_ids.add(event.source_code)
            n_events_loaded += 1
        session.commit()
        print(f"  [{i+1}/{len(roster)}] {name}: {len(events)} events seen, {len(loaded_event_ids)} unique so far", end="\r")

    fetcher.close()
    print(f"\n\nLoaded {n_events_loaded} unique comp_events.")

    rows = session.execute(
        select(CompEvent.raw_title, Result, Entry, Partnership)
        .select_from(Result)
        .join(Entry, Result.entry_id == Entry.id)
        .join(Partnership, Entry.partnership_id == Partnership.id)
        .join(CompEvent, Result.comp_event_id == CompEvent.id)
        .where(CompEvent.competition_id == competition.id)
        .order_by(CompEvent.raw_title, Result.placement_low)
    ).all()
    print(f"\n{len(rows)} total results across {len(loaded_event_ids)} events. Sample:\n")
    current_title = None
    shown = 0
    for raw_title, result, entry, partnership in rows:
        if shown >= 40:
            break
        if raw_title != current_title:
            current_title = raw_title
            print(f"\n-- {raw_title} --")
        leader = session.get(Person, partnership.leader_id)
        follower = session.get(Person, partnership.follower_id) if partnership.follower_id else None
        names = f"{leader.display_name} - {follower.display_name}" if follower else leader.display_name
        print(f"  {result.placement_low}  {names}  (bib {entry.competitor_no})")
        shown += 1


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "1612")
