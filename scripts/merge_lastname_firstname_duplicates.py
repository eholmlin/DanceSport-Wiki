"""One-off bulk merge of "Lastname, Firstname" duplicate Person rows.

Comp Manager's person directory uses "Lastname, Firstname" (see
dsr.parse.comp_mngr.parse_person_directory), unlike WDSF/NDCA's
"Firstname Lastname" -- entity resolution matches by exact string, so a
person already known from another source got a second, duplicate Person
row every time they appeared in Comp Manager data (real cases found and
merged individually before this script existed: Arsenii Moroz, Mikaela
Holmlin, Sofia Chubay).

Only merges the unambiguous case: a comp_mngr-sourced alias matching the
strict "Lastname, Firstname" shape (exactly one comma, no "&"/"and" --
excludes formation-team/multi-person aliases like "Andrea, Justin, Chris
and Jeremy") whose reversed "Firstname Lastname" string matches EXACTLY
ONE other Person.display_name in the whole database, case-insensitively.
Always keeps the non-comma ("Firstname Lastname") person and merges the
comma-formatted duplicate into it, regardless of source or which side has
more data -- that's the canonical display format every other source
already uses.

Deliberately does not touch:
- No reversed-name match at all: nothing to merge.
- 2+ reversed-name matches: genuinely ambiguous (could be different real
  people sharing a name, or further undetected duplicates) -- needs
  human review, not a guess.

Usage:
    python scripts/merge_lastname_firstname_duplicates.py --dry-run
    python scripts/merge_lastname_firstname_duplicates.py
"""
from __future__ import annotations

import argparse
import re

from dsr.db import get_session
from dsr.models import Person, PersonAlias
from dsr.resolve.queue import merge_people

_LASTNAME_FIRSTNAME = re.compile(r"^([A-Za-z'\-]+),\s*([A-Za-z'\-\s]+)$")


def find_candidates(session) -> tuple[list[tuple[int, int, str]], int, int]:
    """Returns (mergeable, no_match_count, ambiguous_count) where mergeable
    is a deduplicated list of (comma_person_id, keep_person_id, raw_name)."""
    comp_mngr_aliases = session.query(PersonAlias).filter(
        PersonAlias.source == "comp_mngr", PersonAlias.raw_name.contains(",")
    ).all()

    all_people = session.query(Person.id, Person.display_name).all()
    by_lower_name: dict[str, list[int]] = {}
    for pid, name in all_people:
        by_lower_name.setdefault(name.strip().lower(), []).append(pid)

    seen_comma_person_ids: set[int] = set()
    mergeable: list[tuple[int, int, str]] = []
    no_match = 0
    ambiguous = 0
    for alias in comp_mngr_aliases:
        if alias.raw_name.count(",") != 1 or "&" in alias.raw_name or " and " in alias.raw_name.lower():
            continue
        match = _LASTNAME_FIRSTNAME.match(alias.raw_name)
        if not match:
            continue
        if alias.person_id in seen_comma_person_ids:
            continue  # already resolved via another alias for the same person

        last, first = match.group(1).strip(), match.group(2).strip()
        reversed_name = f"{first} {last}".strip().lower()
        matches = [pid for pid in by_lower_name.get(reversed_name, []) if pid != alias.person_id]

        if len(matches) == 0:
            no_match += 1
        elif len(matches) == 1:
            seen_comma_person_ids.add(alias.person_id)
            mergeable.append((alias.person_id, matches[0], alias.raw_name))
        else:
            ambiguous += 1

    return mergeable, no_match, ambiguous


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="report counts without merging")
    args = parser.parse_args()

    session = get_session()
    mergeable, no_match, ambiguous = find_candidates(session)
    print(f"Unambiguous merges: {len(mergeable)}")
    print(f"No duplicate found: {no_match}")
    print(f"Ambiguous (skipped): {ambiguous}")

    if args.dry_run:
        return

    merged = 0
    failures: list[tuple[str, str]] = []
    for i, (comma_person_id, keep_person_id, raw_name) in enumerate(mergeable):
        try:
            merge_people(session, keep_person_id=keep_person_id, remove_person_id=comma_person_id)
            session.commit()
            merged += 1
        except Exception as exc:  # noqa: BLE001 -- one bad merge must not kill the batch
            session.rollback()
            failures.append((raw_name, f"{type(exc).__name__}: {exc}"))
        if (i + 1) % 500 == 0:
            print(f"  ... {i + 1}/{len(mergeable)} processed", flush=True)

    print(f"\nDone. Merged {merged}/{len(mergeable)}. {len(failures)} failed.")
    for raw_name, msg in failures[:20]:
        print(f"  FAILED: {raw_name!r} -- {msg}")


if __name__ == "__main__":
    main()
