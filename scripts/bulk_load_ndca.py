"""Bulk-load NDCA Premier competitions within a calendar-date range.

No event index exists for this source (see docs/ndca-format-notes.md), so
loading one competition completely means fetching every one of its
competitors individually -- much higher per-competition request cost than
WDSF's per-comp_event approach. Resilient per-competition: one bad
competition is logged and skipped rather than killing the whole run.

Usage:
    python scripts/bulk_load_ndca.py --start 2026-01-01 --end 2026-08-02
"""
from __future__ import annotations

import argparse
import datetime as dt

from dsr.db import get_session
from dsr.fetch.client import PoliteFetcher
from dsr.fetch.storage import save_raw_document
from dsr.load.ndca import load_event
from dsr.load.wdsf import load_competition
from dsr.parse.ndca import ParseError, parse_competition, parse_competitor_feed, parse_roster

BASE = "https://ndcapremier.com"
SEASONS_TO_SCAN = list(range(1, 50))  # generous range; unknown/empty seasons 404 or error harmlessly.
# NDCA's season numbers don't map 1:1 to calendar years -- they overlap and
# have gaps (e.g. season 19 covers ~Dec 2020-Dec 2021, season 20 covers
# ~Nov 2022-Jan 2023, nothing cleanly fills mid-2022) -- so this range is
# intentionally wide; discover_competitions()'s own date-range filter on
# each event's actual Start_Date is what determines real inclusion, not
# this list. Confirmed season 1 = 2006, season 15+ reaches into 2019-2021.


def discover_competitions(fetcher: PoliteFetcher, session, start: dt.date, end: dt.date) -> list[tuple[str, str, dt.date]]:
    seen_cyi: set[int] = set()
    found: list[tuple[str, str, dt.date]] = []
    for season in SEASONS_TO_SCAN:
        url = f"{BASE}/feed/compyears/?season={season}"
        resp = fetcher.get(url)
        if resp.status_code != 200:
            continue
        save_raw_document(session, source="ndca_premier", url=url, content=resp.content, http_status=resp.status_code)
        try:
            payload = resp.json()
        except ValueError:
            continue
        if payload.get("Status") != 1:
            continue
        for e in payload.get("Events") or []:
            if not e.get("Publish_Results"):
                continue
            cyi = e.get("Comp_Year_ID")
            if cyi in seen_cyi:
                continue
            try:
                comp_start = dt.datetime.strptime(e["Start_Date"], "%m/%d/%Y").date()
            except (KeyError, ValueError):
                continue
            if start <= comp_start <= end:
                seen_cyi.add(cyi)
                found.append((str(cyi), e.get("Competition_Name", ""), comp_start))
    found.sort(key=lambda t: t[2])
    return found


def load_one_competition(fetcher: PoliteFetcher, session, cyi: str) -> int:
    """Returns the number of unique comp_events loaded."""
    meta_url = f"{BASE}/feed/compyears/?cyi={cyi}"
    meta_resp = fetcher.get(meta_url)
    if meta_resp.status_code != 200:
        return 0
    save_raw_document(session, source="ndca_premier", url=meta_url, content=meta_resp.content, http_status=meta_resp.status_code)
    competition_staging = parse_competition(meta_resp.content)
    competition = load_competition(session, competition_staging)

    roster_url = f"{BASE}/feed/results/?cyi={cyi}"
    roster_resp = fetcher.get(roster_url)
    if roster_resp.status_code != 200:
        return 0
    save_raw_document(session, source="ndca_premier", url=roster_url, content=roster_resp.content, http_status=roster_resp.status_code)
    try:
        roster = parse_roster(roster_resp.content)
    except ParseError:
        return 0

    loaded_event_ids: set[str] = set()
    for competitor_id, _name in roster:
        url = f"{BASE}/feed/results/?cyi={cyi}&id={competitor_id}"
        resp = fetcher.get(url)
        if resp.status_code != 200:
            continue
        save_raw_document(session, source="ndca_premier", url=url, content=resp.content, http_status=resp.status_code)
        try:
            events = parse_competitor_feed(resp.content)
        except ParseError:
            continue
        for event in events:
            if event.source_code in loaded_event_ids:
                continue
            load_event(session, competition, event)
            loaded_event_ids.add(event.source_code)
        session.commit()
    return len(loaded_event_ids)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=str, required=True, help="YYYY-MM-DD")
    parser.add_argument("--end", type=str, required=True, help="YYYY-MM-DD")
    args = parser.parse_args()
    start = dt.date.fromisoformat(args.start)
    end = dt.date.fromisoformat(args.end)

    session = get_session()
    fetcher = PoliteFetcher()

    print(f"Discovering NDCA competitions between {start} and {end}...")
    competitions = discover_competitions(fetcher, session, start, end)
    session.commit()
    print(f"Found {len(competitions)} competitions.")

    loaded = 0
    failures: list[tuple[str, str, str]] = []
    for i, (cyi, name, comp_date) in enumerate(competitions):
        try:
            n = load_one_competition(fetcher, session, cyi)
        except Exception as exc:  # noqa: BLE001 -- one bad competition must not kill the batch
            session.rollback()
            failures.append((cyi, name, f"{type(exc).__name__}: {exc}"))
            print(f"  (ERROR, skipped) [{i+1}/{len(competitions)}] {name} ({comp_date}) -- {type(exc).__name__}: {exc}")
            continue
        loaded += 1
        print(f"[{i+1}/{len(competitions)}] {name} ({comp_date}) -> {n} comp_events loaded")

    fetcher.close()
    print(f"\nDone. Loaded {loaded}/{len(competitions)} competitions. {len(failures)} failed.")
    for cyi, name, msg in failures:
        print(f"  FAILED: {name} (cyi={cyi})\n    {msg}")


if __name__ == "__main__":
    main()
