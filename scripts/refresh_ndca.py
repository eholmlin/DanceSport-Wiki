"""Incremental refresh for NDCA Premier.

Finds (a) newly-published competitions we haven't loaded yet and (b) already-
loaded competitions whose results were revised since we last loaded them
(using the source's own Publish_Dates.Results timestamp -- see
Competition.source_updated_at), and only re-fetches full rosters for those.
Everything else is skipped: a single lightweight per-season listing request
tells us what changed, instead of re-fetching every known competition's full
roster on every run.

Meant to be run periodically (e.g. daily/weekly via cron or a scheduled task)
as the ongoing way to keep the database current without ever needing a
from-scratch rebuild.

Usage:
    python scripts/refresh_ndca.py
    python scripts/refresh_ndca.py --seasons 42,43
"""
from __future__ import annotations

import argparse

from sqlalchemy import select

from dsr.db import get_session
from dsr.fetch.client import PoliteFetcher
from dsr.fetch.storage import save_raw_document
from dsr.models import Competition
from dsr.parse.ndca import ParseError, parse_season_listing

from bulk_load_ndca import BASE, SEASONS_TO_SCAN, load_one_competition


def find_changed_competitions(fetcher: PoliteFetcher, session, seasons: list[int]) -> list[tuple[str, str]]:
    """Returns [(cyi, reason), ...] for competitions that need a (re)load."""
    known = {
        c.source_code: c for c in session.scalars(select(Competition).where(Competition.source == "ndca_premier")).all()
    }

    to_refresh: list[tuple[str, str]] = []
    seen_cyi: set[str] = set()
    for season in seasons:
        url = f"{BASE}/feed/compyears/?season={season}"
        resp = fetcher.get(url)
        if resp.status_code != 200:
            continue
        save_raw_document(session, source="ndca_premier", url=url, content=resp.content, http_status=resp.status_code)
        try:
            staged = parse_season_listing(resp.content)
        except ParseError:
            continue
        for comp in staged:
            if comp.source_code in seen_cyi:
                continue
            seen_cyi.add(comp.source_code)
            existing = known.get(comp.source_code)
            if existing is None:
                to_refresh.append((comp.source_code, "new competition"))
            elif comp.source_updated_at is not None and (
                existing.source_updated_at is None or comp.source_updated_at > existing.source_updated_at
            ):
                to_refresh.append(
                    (comp.source_code, f"results updated ({existing.source_updated_at} -> {comp.source_updated_at})")
                )
    session.commit()
    return to_refresh


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--seasons", type=str, default=None, help="comma-separated season numbers; default scans a broad known range"
    )
    args = parser.parse_args()
    seasons = [int(s) for s in args.seasons.split(",")] if args.seasons else SEASONS_TO_SCAN

    session = get_session()
    fetcher = PoliteFetcher()

    print(f"Checking {len(seasons)} season(s) for new or updated competitions...")
    to_refresh = find_changed_competitions(fetcher, session, seasons)
    print(f"{len(to_refresh)} competition(s) need loading.\n")

    for cyi, reason in to_refresh:
        print(f"[{cyi}] {reason}")
        try:
            n = load_one_competition(fetcher, session, cyi)
        except Exception as exc:  # noqa: BLE001 -- one bad competition must not kill the run
            session.rollback()
            print(f"  ERROR: {type(exc).__name__}: {exc}")
            continue
        print(f"  -> {n} comp_events loaded")

    fetcher.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
