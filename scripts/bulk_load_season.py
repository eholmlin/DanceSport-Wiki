"""M2: bulk-load one season, one circuit (spec milestone M2: "~15 events").

Circuit chosen: WDSF Adult Latin + Adult Standard couple events (the closest
WDSF analogue to a single coherent amateur circuit) across calendar year 2024
-- the most recent fully-completed season relative to today's date.

Pipeline per competition: fetch event page -> parse -> load competition +
qualifying comp_events -> for each comp_event, fetch+parse+load ranking,
officials, marks, final. Every page goes through PoliteFetcher (rate-limited,
descriptive UA) and save_raw_document (raw-first, idempotent) before parsing.

Usage:
    python scripts/bulk_load_season.py --year 2024 --target 15
"""
from __future__ import annotations

import argparse
import re

from sqlalchemy import select

from dsr.db import get_session
from dsr.fetch.client import PoliteFetcher
from dsr.fetch.storage import save_raw_document
from dsr.load.wdsf import load_comp_event, load_competition, load_marks, load_officials, load_ranking_page
from dsr.models import CompEvent
from dsr.parse.wdsf import (
    ParseError,
    parse_event_page,
    parse_final_page,
    parse_marks_page,
    parse_officials_page,
    parse_ranking_page,
)

# Bare worlddancesport.org 301s to www.worlddancesport.org but the redirect
# target DROPS the query string -- e.g. .../Calendar/Results?Month=10&Year=2024
# redirects to just .../Calendar/Results (today's date). Always hit www.
# directly to avoid silently landing on the wrong page.
BASE = "https://www.worlddancesport.org"
QUALIFYING_TITLE_RE = re.compile(r"\bAdult\b.*\b(Latin|Standard)\b|\b(Latin|Standard)\b.*\bAdult\b", re.I)
SOLO_TITLE_RE = re.compile(r"\bSolo\b", re.I)


_EVENT_ID_RE = re.compile(r"-(\d+)$")


def discover_event_urls(fetcher: PoliteFetcher, session, year: int) -> list[str]:
    """Scan every month of `year`'s results calendar for /Events/... links.

    The same event sometimes appears with two slug variants for the same
    trailing numeric id (e.g. with vs without an end-date segment) -- dedupe
    on that id so we don't load one competition twice under two source_codes.
    """
    urls: list[str] = []
    seen_ids: set[str] = set()
    for month in range(1, 13):
        url = f"{BASE}/Calendar/Results?Month={month}&Year={year}"
        response = fetcher.get(url)
        save_raw_document(session, source="wdsf", url=url, content=response.content, http_status=response.status_code)
        for match in re.finditer(r'href="(/Events/[^"#]+)', response.text):
            path = match.group(1)
            id_match = _EVENT_ID_RE.search(path)
            if id_match is None:
                continue  # e.g. /Events/Granting -- not a dated event page
            event_id = id_match.group(1)
            if event_id not in seen_ids:
                seen_ids.add(event_id)
                urls.append(f"{BASE}{path}")
    return urls


def is_qualifying(raw_title: str) -> bool:
    """Adult Latin/Standard *couple* events -- excludes Solo so the circuit
    stays one coherent competing-unit type (couples), matching the spec's
    "the partnership is the competing unit" framing rather than mixing in
    solo results here (those load fine too, just via a separate pass)."""
    return bool(QUALIFYING_TITLE_RE.search(raw_title)) and not SOLO_TITLE_RE.search(raw_title)


def load_one_competition(fetcher: PoliteFetcher, session, event_url: str) -> int:
    """Returns the number of qualifying comp_events loaded for this competition."""
    response = fetcher.get(event_url)
    if response.status_code != 200:
        return 0
    save_raw_document(session, source="wdsf", url=event_url, content=response.content, http_status=response.status_code)
    try:
        competition_staging, comp_event_refs = parse_event_page(response.content, event_url)
    except ParseError:
        return 0
    competition = load_competition(session, competition_staging)

    loaded = 0
    for ref in comp_event_refs:
        if not is_qualifying(ref.raw_title):
            continue

        ranking_url = f"{BASE}{ref.ranking_url}"
        ranking_resp = fetcher.get(ranking_url)
        if ranking_resp.status_code != 200:
            continue
        save_raw_document(
            session, source="wdsf", url=ranking_url, content=ranking_resp.content, http_status=ranking_resp.status_code
        )
        try:
            ranking_page = parse_ranking_page(ranking_resp.content)
        except ParseError:
            continue

        comp_event = load_comp_event(session, competition, ref)
        entry_by_competitor_no = load_ranking_page(session, "wdsf", competition, comp_event, ranking_page)

        officials_url = f"{BASE}{ref.officials_url}"
        officials_resp = fetcher.get(officials_url)
        letter_to_person_id = {}
        if officials_resp.status_code == 200:
            save_raw_document(
                session,
                source="wdsf",
                url=officials_url,
                content=officials_resp.content,
                http_status=officials_resp.status_code,
            )
            try:
                officials = parse_officials_page(officials_resp.content)
                letter_to_person_id = load_officials(session, "wdsf", officials)
            except ParseError:
                pass

        if letter_to_person_id and ranking_page.rounds:
            marks_url = f"{BASE}{ref.marks_url}"
            marks_resp = fetcher.get(marks_url)
            if marks_resp.status_code == 200:
                save_raw_document(
                    session, source="wdsf", url=marks_url, content=marks_resp.content, http_status=marks_resp.status_code
                )
                try:
                    recall_marks = parse_marks_page(marks_resp.content)
                    load_marks(session, comp_event, entry_by_competitor_no, letter_to_person_id, recall_marks)
                except ParseError:
                    pass

            final_url = f"{BASE}{ref.final_url}"
            final_resp = fetcher.get(final_url)
            if final_resp.status_code == 200:
                save_raw_document(
                    session, source="wdsf", url=final_url, content=final_resp.content, http_status=final_resp.status_code
                )
                try:
                    final_marks = parse_final_page(final_resp.content)
                    load_marks(session, comp_event, entry_by_competitor_no, letter_to_person_id, final_marks)
                except ParseError:
                    pass

        session.commit()
        loaded += 1
    return loaded


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--year", type=int, default=2024)
    parser.add_argument("--target", type=int, default=15, help="stop once this many competitions have loaded >=1 qualifying comp_event")
    args = parser.parse_args()

    session = get_session()
    fetcher = PoliteFetcher()

    print(f"Discovering event URLs for {args.year}...")
    event_urls = discover_event_urls(fetcher, session, args.year)
    session.commit()
    print(f"Found {len(event_urls)} events in {args.year}.")

    loaded_competitions = 0
    failures: list[tuple[str, str]] = []
    for i, event_url in enumerate(event_urls):
        if loaded_competitions >= args.target:
            break
        try:
            n = load_one_competition(fetcher, session, event_url)
        except Exception as exc:  # noqa: BLE001 -- one bad competition must not kill the batch
            session.rollback()
            failures.append((event_url, f"{type(exc).__name__}: {exc}"))
            print(f"  (ERROR, skipped) {event_url} -- {type(exc).__name__}: {exc}")
            continue
        if n > 0:
            loaded_competitions += 1
            print(f"[{loaded_competitions}/{args.target}] {event_url} -> {n} comp_event(s) loaded")
        else:
            print(f"  (skip, no qualifying comp_events) {event_url}")

    fetcher.close()
    print(f"\nDone. Loaded {loaded_competitions} competitions. {len(failures)} failed.")
    for url, msg in failures:
        print(f"  FAILED: {url}\n    {msg}")


if __name__ == "__main__":
    main()
