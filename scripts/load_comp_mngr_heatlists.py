"""Refresh heat lists for events published on Comp Manager (comp-mngr.com)
rather than NDCA Premier -- see dsr.load.comp_mngr_heatlist.HEATLIST_EVENTS
for the events covered (add one there when a new event publishes its heat
list on comp-mngr.com). Safe/idempotent to re-run as the schedule shifts.

Usage:
    python scripts/load_comp_mngr_heatlists.py
    python scripts/load_comp_mngr_heatlists.py --slug ohiostar2026
"""
from __future__ import annotations

import argparse
import datetime as dt

from dsr.db import get_session
from dsr.fetch.client import PoliteFetcher
from dsr.load.comp_mngr_heatlist import HEATLIST_EVENTS, events_to_refresh, load_comp_mngr_heatlist


def run(session, fetcher, today: dt.date | None = None, slug: str | None = None) -> int:
    """Returns how many events failed to load."""
    today = today or dt.date.today()
    events = [e for e in HEATLIST_EVENTS if e.slug == slug] if slug else events_to_refresh(today)
    failures = 0
    print(f"Comp Manager heat lists: {len(events)} event(s) to refresh.")
    for event in events:
        try:
            n = load_comp_mngr_heatlist(session, fetcher, event)
        except Exception as exc:  # noqa: BLE001 -- one bad event must not kill the batch
            session.rollback()
            failures += 1
            print(f"  (ERROR, skipped) {event.name} -- {type(exc).__name__}: {exc}")
            continue
        print(f"  {event.name} ({event.slug}) -> {n} scheduled_heat rows loaded")
    return failures


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--slug", type=str, default=None, help="refresh just this event, whatever its dates")
    args = parser.parse_args()
    session = get_session()
    fetcher = PoliteFetcher()
    try:
        run(session, fetcher, slug=args.slug)
    finally:
        fetcher.close()


if __name__ == "__main__":
    main()
