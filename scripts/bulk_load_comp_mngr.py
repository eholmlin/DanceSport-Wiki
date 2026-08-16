"""Bulk-load Comp Manager (comp-mngr.com) competitions by slug.

No calendar/discovery feed exists for this source (unlike NDCA Premier's
compyears feed) -- slugs were found one at a time during the gap-analysis
investigation (docs/comp-mngr-format-notes.md) by checking each gapped
competition's own website for a comp-mngr.com results link, then
confirming the exact `<slug>` by fetching that event directory's listing.
KNOWN_SLUGS below is that confirmed set; extend it as more are found
rather than trying to guess/crawl for new ones (comp-mngr.com has no
site-wide index -- see the investigation notes).

Pipeline per competition: fetch its directory listing (-> dates) +
ScoresheetsByPerson index page (-> name, judges, person directory) +
scoresheetsbyperson.dat (-> every round of every event) -> load
competition -> load_officials once -> load_event once per (event, round).
Resilient per-competition: one bad competition is logged and skipped
rather than killing the whole run, same pattern as bulk_load_ndca.py.

Usage:
    python scripts/bulk_load_comp_mngr.py
    python scripts/bulk_load_comp_mngr.py --slugs tristate2023,tristate2024
"""
from __future__ import annotations

import argparse
import re

from dsr.db import get_session
from dsr.fetch.client import PoliteFetcher
from dsr.fetch.storage import save_raw_document
from dsr.load.comp_mngr import load_event
from dsr.load.wdsf import load_competition, load_officials
from dsr.parse.comp_mngr import (
    ParseError,
    parse_competition,
    parse_judges,
    parse_person_directory,
    parse_scoresheets_dat,
)

BASE = "http://www.comp-mngr.com"

# (slug, human label) -- label is cosmetic only (progress output); the
# real competition name always comes from the source itself
# (parse_competition_name), matching every other source in this project.
KNOWN_SLUGS = [
    ("newyorkdf2023", "New York Dance Festival 2023"),
    ("newyorkdf2024", "New York Dance Festival 2024"),
    ("tristate2023", "Tri-State Challenge 2023"),
    ("tristate2024", "Tri-State Challenge 2024"),
    ("tristate2025", "Tri-State Challenge 2025"),
    ("wisconsin2023", "Wisconsin State Dance Championships 2023"),
    ("wisconsin2024", "Wisconsin State Dance Championships 2024"),
    ("wisconsin2025", "Wisconsin State Dance Championships 2025"),
    ("volstdc2023", "Volunteer State Dance Challenge 2023"),
    ("paragon2023", "Paragon Open Dancesport Championships 2023"),
    ("paragon2024", "Paragon Open Dancesport Championships 2024"),
    ("patriot2024", "Patriot Dance Festival 2024"),
    ("myusdc2023", "United States Dance Championships 2023"),
    ("millennium2023", "Millennium Dancesport Championships 2023"),
    ("cbclassic23", "Cincinnati Ballroom Classic 2023"),
    ("calchic23", "California Chic Classic 2023"),
    ("capital2023", "Capital Dance Championships 2023"),
    ("capital2024", "Capital Dance Championships 2024"),
    ("firstcoast23", "First Coast Classic Dancesport Championship 2023"),
    ("firstcoast24", "First Coast Classic Dancesport Championship 2024"),
    ("tropicana23", "Tropicana Dance Challenge 2023"),
    ("tropicana24", "Tropicana Dance Challenge 2024"),
    # Holiday Dance Classic (Las Vegas, December): found via
    # holidaydanceclassic.com/category/results/'s "View on CompMngr"
    # outbound link for each year, during the same gap-analysis
    # investigation that originally looked at dancecomp.io for this
    # competition -- comp-mngr.com turns out to have the same underlying
    # data directly, with full per-judge marks dancecomp.io's
    # placement-only API doesn't expose. 2021 ("holiday2021") lives on
    # the old compmngr.com domain (no dash), which 503'd when checked --
    # left out for now, worth retrying later.
    ("holiday2022", "Holiday Dance Classic 2022"),
    ("holiday2023", "Holiday Dance Classic 2023"),
    ("holiday2024", "Holiday Dance Classic 2024"),
    ("holiday2025", "Holiday Dance Classic 2025"),
]


def _fetch(fetcher: PoliteFetcher, session, url: str) -> bytes | None:
    resp = fetcher.get(url)
    if resp.status_code != 200:
        return None
    save_raw_document(session, source="comp_mngr", url=url, content=resp.content, http_status=resp.status_code)
    return resp.content


def load_one_competition(fetcher: PoliteFetcher, session, slug: str) -> tuple[int, int, int]:
    """Returns (comp_events_loaded, marks_loaded, blocks_skipped)."""
    base_url = f"{BASE}/{slug}/"
    listing = _fetch(fetcher, session, base_url)

    index_url = f"{base_url}{slug.capitalize()}_ScoresheetsByPerson.htm"
    index = _fetch(fetcher, session, index_url)
    if index is None:
        # Filename casing on the actual site doesn't always match the
        # slug's capitalize()'d guess -- fall back to the directory
        # listing to find the real filename before giving up.
        if listing is None:
            raise ParseError(f"could not fetch directory listing or index page for slug={slug!r}")
        match = re.search(r'href="([^"]*ScoresheetsByPerson\.htm)"', listing.decode("utf-8", errors="replace"))
        if not match:
            raise ParseError(f"no ScoresheetsByPerson.htm link found in directory listing for slug={slug!r}")
        index_url = base_url + match.group(1)
        index = _fetch(fetcher, session, index_url)
        if index is None:
            raise ParseError(f"could not fetch index page at {index_url}")

    dat_url = f"{base_url}{slug}_scoresheetsbyperson.dat"
    dat = _fetch(fetcher, session, dat_url)
    if dat is None:
        raise ParseError(f"could not fetch {dat_url}")

    competition_staging = parse_competition(index, listing, source_code=slug, url=base_url)
    competition = load_competition(session, competition_staging)

    judges = parse_judges(index)
    judge_letter_to_person_id = load_officials(session, "comp_mngr", judges)

    directory = parse_person_directory(index)
    events, skipped = parse_scoresheets_dat(dat, directory)

    loaded_titles: set[str] = set()
    total_marks = 0
    for event in events:
        load_event(session, competition, event, judge_letter_to_person_id)
        loaded_titles.add(event.raw_title)
        total_marks += len(event.marks)
    session.commit()
    return len(loaded_titles), total_marks, skipped


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--slugs", type=str, default=None, help="comma-separated slugs; defaults to KNOWN_SLUGS")
    args = parser.parse_args()

    if args.slugs:
        wanted = set(args.slugs.split(","))
        targets = [(slug, label) for slug, label in KNOWN_SLUGS if slug in wanted]
    else:
        targets = KNOWN_SLUGS

    session = get_session()
    fetcher = PoliteFetcher()

    loaded = 0
    failures: list[tuple[str, str, str]] = []
    for i, (slug, label) in enumerate(targets):
        try:
            n_events, n_marks, n_skipped = load_one_competition(fetcher, session, slug)
        except Exception as exc:  # noqa: BLE001 -- one bad competition must not kill the batch
            session.rollback()
            failures.append((slug, label, f"{type(exc).__name__}: {exc}"))
            print(f"  (ERROR, skipped) [{i+1}/{len(targets)}] {label} ({slug}) -- {type(exc).__name__}: {exc}", flush=True)
            continue
        loaded += 1
        print(
            f"[{i+1}/{len(targets)}] {label} ({slug}) -> {n_events} comp_events, "
            f"{n_marks:,} marks loaded ({n_skipped} blocks skipped)",
            flush=True,
        )

    fetcher.close()
    print(f"\nDone. Loaded {loaded}/{len(targets)} competitions. {len(failures)} failed.")
    for slug, label, msg in failures:
        print(f"  FAILED: {label} ({slug})\n    {msg}")


if __name__ == "__main__":
    main()
