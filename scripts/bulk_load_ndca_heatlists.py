"""Bulk-load NDCA Premier heat lists (pre-competition schedules) for
competitions starting within a date range -- see dsr.models.ScheduledHeat
and dsr.parse.ndca.parse_heatlist_attendee for why this is a separate
pipeline from bulk_load_ndca.py's results loading.

Same per-attendee fetch-cost shape as results (no event-index endpoint --
see docs/ndca-format-notes.md), but discovery filters on Publish_Heatlists
instead of Publish_Results, and defaults to a near-term window (today
onward) rather than requiring explicit dates, since heat lists are only
ever meaningful for upcoming/in-progress competitions. Safe/idempotent to
re-run as a schedule shifts before the event or as the event proceeds --
see load_scheduled_heat's natural key.

Usage:
    python scripts/bulk_load_ndca_heatlists.py
    python scripts/bulk_load_ndca_heatlists.py --start 2026-09-01 --end 2026-09-30
"""
from __future__ import annotations

import argparse
import datetime as dt

from dsr.db import get_session
from dsr.fetch.client import PoliteFetcher
from dsr.fetch.storage import save_raw_document
from dsr.load.heatlist import drop_finished_competitions_with_results, load_scheduled_heat
from dsr.load.wdsf import load_competition
from dsr.parse.ndca import SOURCE, ParseError, parse_competition, parse_heatlist_attendee, parse_roster

BASE = "https://ndcapremier.com"
SEASONS_TO_SCAN = list(range(1, 50))  # see bulk_load_ndca.py -- season numbers don't map 1:1 to calendar years


def discover_competitions(fetcher: PoliteFetcher, session, start: dt.date, end: dt.date) -> list[tuple[str, str, dt.date]]:
    seen_cyi: set[int] = set()
    found: list[tuple[str, str, dt.date]] = []
    for season in SEASONS_TO_SCAN:
        url = f"{BASE}/feed/compyears/?season={season}"
        resp = fetcher.get(url)
        if resp.status_code != 200:
            continue
        save_raw_document(session, source=SOURCE, url=url, content=resp.content, http_status=resp.status_code)
        try:
            payload = resp.json()
        except ValueError:
            continue
        if payload.get("Status") != 1:
            continue
        for e in payload.get("Events") or []:
            if not e.get("Publish_Heatlists"):
                continue
            cyi = e.get("Comp_Year_ID")
            if cyi in seen_cyi:
                continue
            try:
                comp_start = dt.datetime.strptime(e["Start_Date"], "%m/%d/%Y").date()
            except (KeyError, ValueError):
                continue
            try:
                comp_end = dt.datetime.strptime(e["End_Date"], "%m/%d/%Y").date()
            except (KeyError, ValueError):
                comp_end = comp_start
            # Overlap test, not "starts within window": a multi-day
            # competition that started before `start` (e.g. run today,
            # the day after it opened) but hasn't finished yet is exactly
            # the case a daily refresh most needs to catch -- its heat
            # list is still being updated live. Filtering on Start_Date
            # alone dropped it from the window the moment its start date
            # passed, even mid-competition.
            if comp_end >= start and comp_start <= end:
                seen_cyi.add(cyi)
                found.append((str(cyi), e.get("Competition_Name", ""), comp_start))
    found.sort(key=lambda t: t[2])
    return found


def load_one_competition(fetcher: PoliteFetcher, session, cyi: str) -> int:
    """Returns the number of scheduled_heat rows loaded/updated."""
    meta_url = f"{BASE}/feed/compyears/?cyi={cyi}"
    meta_resp = fetcher.get(meta_url)
    if meta_resp.status_code != 200:
        return 0
    save_raw_document(session, source=SOURCE, url=meta_url, content=meta_resp.content, http_status=meta_resp.status_code)
    competition_staging = parse_competition(meta_resp.content)
    competition = load_competition(session, competition_staging)

    roster_url = f"{BASE}/feed/heatlists/?cyi={cyi}"
    roster_resp = fetcher.get(roster_url)
    if roster_resp.status_code != 200:
        return 0
    save_raw_document(session, source=SOURCE, url=roster_url, content=roster_resp.content, http_status=roster_resp.status_code)
    try:
        roster = parse_roster(roster_resp.content)
    except ParseError:
        return 0

    loaded = 0
    for attendee_id, _name in roster:
        url = f"{BASE}/feed/heatlists/?cyi={cyi}&id={attendee_id}"
        resp = fetcher.get(url)
        if resp.status_code != 200:
            continue
        save_raw_document(session, source=SOURCE, url=url, content=resp.content, http_status=resp.status_code)
        try:
            staged_heats = parse_heatlist_attendee(resp.content)
        except ParseError:
            continue
        for staged in staged_heats:
            load_scheduled_heat(session, SOURCE, competition, staged)
            loaded += 1
        session.commit()
    return loaded


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=str, default=None, help="YYYY-MM-DD, default: today")
    parser.add_argument("--end", type=str, default=None, help="YYYY-MM-DD, default: today + 30 days")
    args = parser.parse_args()
    today = dt.date.today()
    start = dt.date.fromisoformat(args.start) if args.start else today
    end = dt.date.fromisoformat(args.end) if args.end else today + dt.timedelta(days=30)

    session = get_session()
    fetcher = PoliteFetcher()

    print(f"Discovering NDCA competitions with published heat lists between {start} and {end}...")
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
        print(f"[{i+1}/{len(competitions)}] {name} ({comp_date}) -> {n} scheduled_heat rows loaded")

    fetcher.close()
    print(f"\nDone. Loaded {loaded}/{len(competitions)} competitions. {len(failures)} failed.")
    for cyi, name, msg in failures:
        print(f"  FAILED: {name} (cyi={cyi})\n    {msg}")

    # A finished competition's heat list is superseded once its results are
    # on file (usually the next day's run, sometimes later if results
    # loading itself runs late) -- see drop_finished_competitions_with_results
    # for why one without results yet is left alone regardless of how long
    # ago it finished. Runs every refresh (not just once) since it's cheap
    # and idempotent: a competition already dropped just won't show up in
    # the scheduled_heat table to check again.
    dropped = drop_finished_competitions_with_results(session)
    if dropped:
        print(f"\nDropped {len(dropped)} finished competition(s) with results now on file:")
        for name, n in dropped:
            print(f"  {name} -- {n} scheduled_heat rows removed")


if __name__ == "__main__":
    main()
