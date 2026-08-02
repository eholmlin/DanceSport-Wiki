"""Fuzzy candidate matching for entity resolution (spec section 5, steps 3-5).

Only reached when the fast paths (external-ID match, exact person_alias hit)
in resolve_person() come up empty -- for WDSF specifically that's rare, since
almost every ranking-page entry carries a stable athlete GUID (external_ref)
that resolves identity outright. This path matters for records that lack one
(most often officials/judges on older page variants) and, longer-term, any
source without a persistent external id (e.g. O2CM).

Known simplification: spec calls for "Jaro-Winkler on both name parts"
(i.e. comparing given-name-to-given-name, family-to-family). We don't
reliably have a given/family split for WDSF names (some are transliterated
"Family Given", others "Given Family", and person.family_name is left NULL
at load time -- see resolve/entities.py). Instead we tokenize, sort tokens,
and run Jaro-Winkler on the whole normalized string, which is order-
insensitive but coarser than a true per-field comparison. Documented here
rather than silently passed off as a full implementation of the spec's wording.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import jellyfish
from sqlalchemy import select
from sqlalchemy.orm import Session

from dsr.models import Partnership, Person, PersonAlias

AUTO_MERGE_THRESHOLD = 0.93
QUEUE_THRESHOLD = 0.75

NAME_WEIGHT = 0.85
PARTNER_BONUS = 0.10
COUNTRY_BONUS = 0.05


def _tokens(name: str) -> list[str]:
    return [t for t in re.split(r"[\s,]+", name.strip()) if t]


def _normalized(name: str) -> str:
    return " ".join(sorted(t.lower() for t in _tokens(name)))


def blocking_keys(name: str) -> set[str]:
    """Soundex of each alphabetic token, order-independent (spec: "family-name
    soundex/metaphone key" -- since we don't reliably know which token is the
    family name, block on all of them instead of just the last)."""
    return {jellyfish.soundex(t) for t in _tokens(name) if t.isalpha()}


def name_similarity(a: str, b: str) -> float:
    return jellyfish.jaro_winkler_similarity(_normalized(a), _normalized(b))


@dataclass
class Candidate:
    person: Person
    score: float
    name_score: float
    shared_partner: bool
    same_country: bool


def find_candidates(
    session: Session,
    *,
    name: str,
    source: str,
    country: str | None = None,
    partner_person_ids: frozenset[int] = frozenset(),
) -> list[Candidate]:
    """Blocking + scoring over existing person_alias rows for `source`.

    Scans all aliases for this source rather than querying by blocking key in
    SQL (blocking keys aren't persisted) -- fine at M2 scale (~15 events), not
    intended to scale to a large multi-source corpus without adding a
    persisted/indexed blocking-key column.
    """
    keys = blocking_keys(name)
    if not keys:
        return []

    aliases = session.scalars(select(PersonAlias).where(PersonAlias.source == source)).all()
    names_by_person: dict[int, list[str]] = {}
    for alias in aliases:
        if blocking_keys(alias.raw_name) & keys:
            names_by_person.setdefault(alias.person_id, []).append(alias.raw_name)

    candidates: list[Candidate] = []
    for person_id, known_names in names_by_person.items():
        person = session.get(Person, person_id)
        if person is None:
            continue
        name_score = max(name_similarity(name, n) for n in known_names)

        shared_partner = False
        if partner_person_ids:
            shared_partner = (
                session.scalar(
                    select(Partnership).where(
                        (
                            (Partnership.leader_id == person_id)
                            & (Partnership.follower_id.in_(partner_person_ids))
                        )
                        | (
                            (Partnership.follower_id == person_id)
                            & (Partnership.leader_id.in_(partner_person_ids))
                        )
                    )
                )
                is not None
            )

        same_country = bool(country and person.country and country == person.country)

        score = NAME_WEIGHT * name_score
        if shared_partner:
            score += PARTNER_BONUS
        if same_country:
            score += COUNTRY_BONUS
        score = min(score, 1.0)

        candidates.append(
            Candidate(
                person=person,
                score=score,
                name_score=name_score,
                shared_partner=shared_partner,
                same_country=same_country,
            )
        )

    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates
