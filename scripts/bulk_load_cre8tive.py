"""Bulk-load Cre8tive Score competitions (International Dance Festival,
Platinum Dance Awards, ...) -- see docs/cre8tive-format-notes.md.

Discovery is one call to the public calendar. Per competition: meta +
competitors + result-index, then one result-detail request per indexed
event (every round of every category). Each response is saved raw before
parsing, and the fetcher is the project's polite one (1.5 s between
requests, identifying User-Agent). Idempotent: re-running upserts.

Usage:
    python scripts/bulk_load_cre8tive.py --list
    python scripts/bulk_load_cre8tive.py --only monday            # substring of id or name
    python scripts/bulk_load_cre8tive.py --only monday --dry-run  # fetch + parse, write nothing
    python scripts/bulk_load_cre8tive.py                          # every competition with results
"""
from __future__ import annotations

import argparse
import json
from urllib.parse import quote

from dsr.db import get_session
from dsr.fetch.client import PoliteFetcher
from dsr.fetch.storage import save_raw_document
from dsr.load.cre8tive import load_event
from dsr.load.wdsf import load_competition, load_officials
from dsr.parse.cre8tive import (
    SOURCE,
    ParseError,
    build_event,
    group_events,
    officials_from,
    parse_calendar,
    parse_competition,
    parse_competitors,
    parse_result_index,
)

API = "https://cre8tive-score.vercel.app/api"
COUNTRY = "United Kingdom"  # every competition on the platform so far is a UK event


def _fetch(fetcher: PoliteFetcher, session, url: str, *, save: bool) -> bytes | None:
    resp = fetcher.get(url)
    if resp.status_code != 200:
        return None
    if save:
        save_raw_document(session, source=SOURCE, url=url, content=resp.content, http_status=resp.status_code)
    return resp.content


def load_one_competition(fetcher: PoliteFetcher, session, comp_id: str, *, dry_run: bool) -> dict:
    snap = f"{API}/public-snapshots/{quote(comp_id)}"
    save = not dry_run

    meta = _fetch(fetcher, session, f"{snap}?kind=meta", save=save)
    competitors = _fetch(fetcher, session, f"{snap}?kind=competitors", save=save)
    index = _fetch(fetcher, session, f"{snap}?kind=result-index", save=save)
    if meta is None or competitors is None or index is None:
        raise ParseError(f"could not fetch meta/competitors/result-index for {comp_id}")

    staging, meta_judges, hide_judge_scores = parse_competition(meta, country=COUNTRY)
    names = parse_competitors(competitors)

    details: list[dict] = []
    for event_id in parse_result_index(index):
        raw = _fetch(fetcher, session, f"{snap}?kind=result-detail&eventId={quote(event_id)}", save=save)
        if raw is None:
            continue
        data = json.loads(raw).get("data")
        if data:  # null until that round's results are published
            details.append(data)

    events = []
    judges: dict[str, str] = dict(meta_judges)
    for sku, raw_title, rows in group_events(details):
        event, used = build_event(sku, raw_title, rows, names, meta_judges, hide_judge_scores=hide_judge_scores)
        events.append(event)
        judges.update(used)

    summary = {
        "events": len(events),
        "rounds": sum(len(e.ranking.rounds) for e in events),
        "entries": sum(len(e.ranking.entries) for e in events),
        "marks": sum(len(e.marks) for e in events),
        "skipped_couples": sum(e.skipped_couples for e in events),
    }
    if dry_run:
        return summary

    competition = load_competition(session, staging)
    judge_person_ids = load_officials(session, SOURCE, officials_from(judges))
    for event in events:
        load_event(session, competition, event, judge_person_ids)
    session.commit()
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--list", action="store_true", help="list the competitions on the platform and exit")
    parser.add_argument("--only", type=str, default=None, help="case-insensitive substring of the competition id or name")
    parser.add_argument("--dry-run", action="store_true", help="fetch and parse, but write nothing (not even raw documents)")
    args = parser.parse_args()

    session = get_session()
    fetcher = PoliteFetcher()

    calendar = _fetch(fetcher, session, f"{API}/public-calendar", save=not args.list and not args.dry_run)
    if calendar is None:
        raise SystemExit("could not fetch the Cre8tive Score calendar")
    entries = parse_calendar(calendar)
    if args.only:
        needle = args.only.lower()
        entries = [e for e in entries if needle in e.id.lower() or needle in e.name.lower()]

    if args.list:
        for e in entries:
            print(f"{e.date}  {e.id}  results={'yes' if e.results_published else 'no'}  judge marks={'hidden' if e.hide_judge_scores else 'shown'}  {e.name}")
        fetcher.close()
        return

    targets = [e for e in entries if e.results_published]
    loaded = 0
    failures: list[tuple[str, str]] = []
    for i, e in enumerate(targets):
        try:
            s = load_one_competition(fetcher, session, e.id, dry_run=args.dry_run)
        except Exception as exc:  # noqa: BLE001 -- one bad competition must not kill the batch
            session.rollback()
            failures.append((e.name, f"{type(exc).__name__}: {exc}"))
            print(f"  (ERROR, skipped) [{i+1}/{len(targets)}] {e.name} -- {type(exc).__name__}: {exc}", flush=True)
            continue
        loaded += 1
        print(
            f"[{i+1}/{len(targets)}] {e.name} -> {s['events']} comp_events, {s['rounds']} rounds, "
            f"{s['entries']} entries, {s['marks']:,} marks ({s['skipped_couples']} couples without a name skipped)"
            + (" [dry run]" if args.dry_run else ""),
            flush=True,
        )

    fetcher.close()
    print(f"\nDone. {'Parsed' if args.dry_run else 'Loaded'} {loaded}/{len(targets)} competitions. {len(failures)} failed.")
    for name, msg in failures:
        print(f"  FAILED: {name}\n    {msg}")


if __name__ == "__main__":
    main()
