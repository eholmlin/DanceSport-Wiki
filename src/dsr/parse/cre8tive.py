"""Parser for Cre8tive Score (cre8tive-result.vercel.app), the scoring
platform behind the International Dance Festival 2026 and the Platinum
Dance Awards -- see docs/cre8tive-format-notes.md for how the public JSON
API (cre8tive-score.vercel.app/api/...) is laid out.

Everything here is a pure function of the JSON bytes the API returned.
One Cre8tive "competition" is one day/session of a festival, so each maps
to one Competition row. Within it, an event's rounds (1R/2R/3R/4R/QF/SF/F)
are separate API events sharing a `sku`; build_event stitches one sku's
rounds back into a single CompEvent's worth of staging data.

Per-judge data:
- Final: each judge's placement per dance (perDanceResults[].results[].judgeMarks,
  aligned with perDanceResults[].judges) -> StagingMark.placement.
- Recall rounds: each judge's `picks` per dance (couple numbers recalled)
  -> StagingMark.recalled True/False for every couple in the round. The
  judge's `hesitations` lists (couples flagged, not counted toward the
  round totals, meaning undocumented) -> StagingHesitation, stored
  separately from the marks.
- A competition with hideJudgeScores set (Platinum Dance Awards) publishes
  no per-judge data at all; only placements are produced there.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
from dataclasses import dataclass, field

from dsr.parse.staging import (
    RankingPage,
    StagingCompetition,
    StagingEntry,
    StagingHesitation,
    StagingMark,
    StagingOfficial,
    StagingPersonRef,
    StagingResult,
    StagingRound,
)

SOURCE = "cre8tive"
PARSER_VERSION = "cre8tive-v1"

RESULTS_SITE = "https://cre8tive-result.vercel.app"

logger = logging.getLogger(__name__)

# API round code -> (round_type label, sort rank). Labels match the ones the
# NDCA/Comp Manager loaders already store ("Final", "Semi-final", ...) so
# the app's round handling needs nothing source-specific.
_ROUNDS = {
    "1R": ("Round 1", 1),
    "2R": ("Round 2", 2),
    "3R": ("Round 3", 3),
    "4R": ("Round 4", 4),
    "QF": ("Quarter-final", 5),
    "SF": ("Semi-final", 6),
    "F": ("Final", 7),
}

# Dance letters -> the "Int'l Waltz" / "Amer. Rumba" names the other
# sources store in Mark.dance, chosen by the event's style.
_DANCE_NAMES = {
    "Standard": {"W": "Int'l Waltz", "T": "Int'l Tango", "V": "Int'l Viennese Waltz", "F": "Int'l Foxtrot", "Q": "Int'l Quickstep"},
    "Latin": {"C": "Int'l Cha Cha", "S": "Int'l Samba", "R": "Int'l Rumba", "P": "Int'l Paso Doble", "J": "Int'l Jive"},
    "Smooth": {"W": "Amer. Waltz", "T": "Amer. Tango", "F": "Amer. Foxtrot", "V": "Amer. Viennese Waltz"},
    "Rhythm": {"C": "Amer. Cha Cha", "R": "Amer. Rumba", "S": "Amer. Swing", "B": "Amer. Bolero", "M": "Amer. Mambo"},
}
_STYLE_WORDS = (("Ballroom", "Standard"), ("Standard", "Standard"), ("Latin", "Latin"), ("Smooth", "Smooth"), ("Rhythm", "Rhythm"))

_TITLE_PREFIX = re.compile(r"^(?:mr|ms|mrs|miss|dr)\.?\s+", re.IGNORECASE)
_AND_SPLIT = re.compile(r"\s+and\s+", re.IGNORECASE)
_WORD = re.compile(r"[^\W\d_]+")


class ParseError(ValueError):
    """Raised when a response doesn't match the expected shape."""


@dataclass
class CalendarEntry:
    id: str
    name: str
    date: dt.date | None
    results_published: bool
    hide_judge_scores: bool


@dataclass
class Cre8tiveEvent:
    """One CompEvent's worth of data (all of one sku's rounds)."""

    sku: str
    raw_title: str
    ranking: RankingPage
    marks: list[StagingMark] = field(default_factory=list)
    hesitations: list[StagingHesitation] = field(default_factory=list)
    skipped_couples: int = 0  # couples in a round with no name in the competitor list


def _json(content: bytes, what: str) -> dict:
    try:
        payload = json.loads(content)
    except ValueError as exc:
        raise ParseError(f"{what}: not JSON ({exc})") from exc
    if not isinstance(payload, dict):
        raise ParseError(f"{what}: expected a JSON object")
    return payload


def _data(content: bytes, what: str):
    payload = _json(content, what)
    if "data" not in payload:
        raise ParseError(f"{what}: no 'data' key")
    return payload["data"]


def _parse_date(raw: str | None) -> dt.date | None:
    """Cre8tive dates are 'YYYY/M/D' with no zero padding."""
    if not raw:
        return None
    try:
        year, month, day = (int(p) for p in raw.split("/"))
        return dt.date(year, month, day)
    except ValueError:
        return None


def _from_ms(value) -> dt.datetime | None:
    if not isinstance(value, (int, float)):
        return None
    return dt.datetime.fromtimestamp(value / 1000, tz=dt.timezone.utc).replace(tzinfo=None)


def parse_calendar(content: bytes) -> list[CalendarEntry]:
    payload = _json(content, "calendar")
    competitions = payload.get("competitions")
    if not isinstance(competitions, list):
        raise ParseError("calendar: no 'competitions' list")
    return [
        CalendarEntry(
            id=c["id"],
            name=c["name"],
            date=_parse_date(c.get("date")),
            results_published=bool(c.get("resultsPublished")),
            hide_judge_scores=bool(c.get("hideJudgeScores")),
        )
        for c in competitions
    ]


def parse_competition(meta_json: bytes, *, country: str | None = None) -> tuple[StagingCompetition, dict[str, str], bool]:
    """Returns (competition, judge id -> name, hide_judge_scores)."""
    meta = _data(meta_json, "meta")
    if not isinstance(meta, dict) or not meta.get("id") or not meta.get("name"):
        raise ParseError("meta: missing id/name")
    date = _parse_date(meta.get("date"))
    competition = StagingCompetition(
        source=SOURCE,
        source_code=meta["id"],
        name=meta["name"],
        start_date=date,
        end_date=date,
        city=None,
        country=country,
        sanctioning_body=None,
        url=f"{RESULTS_SITE}/event/{meta['id']}/results",
        source_updated_at=_from_ms(meta.get("resultsPublishedAt") or meta.get("updatedAt")),
    )
    judge_names = {str(k): _clean_judge_name(v) for k, v in (meta.get("judgeNames") or {}).items()}
    return competition, judge_names, bool(meta.get("hideJudgeScores"))


def parse_result_index(content: bytes) -> list[str]:
    data = _data(content, "result-index")
    if not isinstance(data, list):
        raise ParseError("result-index: expected a list")
    return [e for e in data if isinstance(e, str)]


def _clean_judge_name(name: str) -> str:
    return _TITLE_PREFIX.sub("", (name or "").strip()).strip()


def _title_case(name: str) -> str:
    return _WORD.sub(lambda m: m.group(0).capitalize(), name)


def _clean_competitor_name(name: str) -> str:
    """Names arrive as 'A AND B' (upper case) or 'A and B' (mixed), with an
    occasional '** ' marker prefix; upper-case parts are title-cased so they
    match the same person's name from other sources. A couple comes back
    normalized to 'A and B'."""
    name = re.sub(r"^\*+\s*", "", name.strip())
    parts = _AND_SPLIT.split(name)
    return " and ".join(_title_case(p) if p.isupper() else p for p in parts)


def parse_competitors(content: bytes) -> dict[tuple[str, str], str]:
    """(sku, bib) -> cleaned display name ('A and B' for a couple)."""
    data = _data(content, "competitors")
    if not isinstance(data, list):
        raise ParseError("competitors: expected a list")
    names: dict[tuple[str, str], str] = {}
    for row in data:
        c = row.get("competitor") or {}
        sku, bib, name = c.get("sku"), c.get("bibNumber"), c.get("name")
        if sku is None or bib is None or not name:
            continue
        names[(str(sku), str(bib))] = _clean_competitor_name(name)
    return names


def _style_of(raw_title: str) -> str | None:
    for word, style in _STYLE_WORDS:
        if word in raw_title:
            return style
    return None


def _dance_name(code: str, style: str | None) -> str:
    return _DANCE_NAMES.get(style or "", {}).get(code, code)


def _split_couple(name: str) -> tuple[StagingPersonRef, StagingPersonRef | None]:
    parts = _AND_SPLIT.split(name)
    if len(parts) == 2 and all(p.strip() for p in parts):
        return StagingPersonRef(name=parts[0].strip()), StagingPersonRef(name=parts[1].strip())
    return StagingPersonRef(name=name), None  # a solo entry


def _int_list(csv: str | None) -> list[str]:
    return [p.strip() for p in (csv or "").split(",") if p.strip()]


def _round_couples(data: dict) -> list[str]:
    """Couple numbers that danced this round, in listed order."""
    result = data.get("result") or {}
    if result.get("overallFinalResults"):
        return [str(r["coupleId"]) for r in result["overallFinalResults"]]
    summary = (data.get("eventScores") or {}).get("roundResultsSummary") or []
    return [str(r["coupleId"]) for r in summary]


def _recall_marks(
    data: dict, label: str, couples: list[str], style: str | None, judge_names: dict[str, str]
) -> tuple[list[StagingMark], list[StagingHesitation], dict[str, str]]:
    event = data.get("event") or {}
    dances = _int_list(event.get("dances"))
    scores = data.get("eventScores") or {}
    marks: list[StagingMark] = []
    hesitations: list[StagingHesitation] = []
    used: dict[str, str] = {}
    couples_in_round = set(couples)
    for judge_id in _int_list(event.get("judges")):
        scored = scores.get(f"judge_{judge_id}")
        # A judge listed for the event but with nothing submitted would
        # otherwise read as "recalled nobody" -- not data, so skip.
        if not scored or not (scored.get("submittedAt") or any((scored.get("picks") or {}).values())):
            continue
        name = judge_names.get(judge_id)
        if not name:
            continue
        used[judge_id] = name
        for index, code in enumerate(dances):
            picked = {str(p) for p in (scored.get("picks") or {}).get(str(index), [])}
            for flagged in (scored.get("hesitations") or {}).get(str(index), []):
                if str(flagged) in couples_in_round:
                    hesitations.append(
                        StagingHesitation(
                            round_label=label,
                            competitor_no=str(flagged),
                            judge_letter=judge_id,
                            dance=_dance_name(code, style),
                        )
                    )
            for couple in couples:
                marks.append(
                    StagingMark(
                        round_label=label,
                        competitor_no=couple,
                        judge_letter=judge_id,
                        dance=_dance_name(code, style),
                        recalled=couple in picked,
                        placement=None,
                    )
                )
    return marks, hesitations, used


def _final_marks(data: dict, label: str, style: str | None) -> tuple[list[StagingMark], dict[str, str]]:
    marks: list[StagingMark] = []
    used: dict[str, str] = {}
    for dance in (data.get("result") or {}).get("perDanceResults") or []:
        judges = dance.get("judges") or []
        for judge in judges:
            used[str(judge["id"])] = _clean_judge_name(judge.get("name", ""))
        for row in dance.get("results") or []:
            for judge, placement in zip(judges, row.get("judgeMarks") or []):
                if not isinstance(placement, int):
                    continue
                marks.append(
                    StagingMark(
                        round_label=label,
                        competitor_no=str(row["coupleId"]),
                        judge_letter=str(judge["id"]),
                        dance=_dance_name(dance.get("danceName", ""), style),
                        recalled=None,
                        placement=placement,
                    )
                )
    return marks, used


def group_events(details: list[dict]) -> list[tuple[str, str, list[dict]]]:
    """Group a competition's result-detail payloads by sku. Returns
    (sku, raw_title, details) per CompEvent. raw_title is the event's
    category; two skus sharing a category within one competition (the
    CompEvent natural key is the title) get the sku appended."""
    by_sku: dict[str, list[dict]] = {}
    for data in details:
        sku = (data.get("event") or {}).get("sku")
        if sku:
            by_sku.setdefault(str(sku), []).append(data)

    def category(rows: list[dict]) -> str:
        event = rows[0]["event"]
        return (event.get("categoryEn") or event.get("category") or str(event["sku"])).strip()

    titles = {sku: category(rows) for sku, rows in by_sku.items()}
    counts: dict[str, int] = {}
    for title in titles.values():
        counts[title] = counts.get(title, 0) + 1
    return [
        (sku, f"{titles[sku]} [{sku}]" if counts[titles[sku]] > 1 else titles[sku], rows)
        for sku, rows in by_sku.items()
    ]


def build_event(
    sku: str,
    raw_title: str,
    details: list[dict],
    names: dict[tuple[str, str], str],
    judge_names: dict[str, str],
    *,
    hide_judge_scores: bool = False,
) -> tuple[Cre8tiveEvent, dict[str, str]]:
    """Stitch one sku's round details (the `data` of each result-detail
    response) into a CompEvent's staging data. Returns (event, judge id ->
    name for every judge that has a mark)."""
    style = _style_of(raw_title)

    # One detail per round: a re-published round keeps the most recent one.
    by_round: dict[str, dict] = {}
    for data in details:
        code = (data.get("event") or {}).get("rounds")
        if code not in _ROUNDS:
            logger.warning("sku %s: unknown round code %r, skipped", sku, code)
            continue
        kept = by_round.get(code)
        if kept is None or (data.get("updatedAt") or 0) >= (kept.get("updatedAt") or 0):
            by_round[code] = data
    ordered = sorted(by_round.items(), key=lambda item: _ROUNDS[item[0]][1])

    staging_rounds: list[StagingRound] = []
    marks: list[StagingMark] = []
    hesitations: list[StagingHesitation] = []
    judges_used: dict[str, str] = {}
    couples_by_round: list[list[str]] = []
    final_ranks: dict[str, int] = {}
    skipped = 0

    for order, (code, data) in enumerate(ordered, start=1):
        label = _ROUNDS[code][0]
        couples = [c for c in _round_couples(data) if (sku, c) in names]
        skipped += len(_round_couples(data)) - len(couples)
        couples_by_round.append(couples)

        recalled_count = None
        if code == "F":
            for row in (data.get("result") or {}).get("overallFinalResults") or []:
                if (sku, str(row["coupleId"])) in names:
                    final_ranks[str(row["coupleId"])] = row["overallFinalRank"]
            round_marks, used = ([], {}) if hide_judge_scores else _final_marks(data, label, style)
            round_hesitations = []
        else:
            summary = (data.get("eventScores") or {}).get("roundResultsSummary") or []
            recalled_count = sum(1 for r in summary if r.get("advanced"))
            round_marks, round_hesitations, used = (
                ([], [], {}) if hide_judge_scores else _recall_marks(data, label, couples, style, judge_names)
            )
        staging_rounds.append(
            StagingRound(round_type=label, round_order=order, entries_in=len(couples), recalled_count=recalled_count)
        )
        marks.extend(m for m in round_marks if (sku, m.competitor_no) in names)
        hesitations.extend(round_hesitations)
        judges_used.update(used)

    seen: set[str] = set()
    entries: list[StagingEntry] = []
    for couples in couples_by_round:
        for couple in couples:
            if couple in seen:
                continue
            seen.add(couple)
            partner_1, partner_2 = _split_couple(names[(sku, couple)])
            entries.append(StagingEntry(competitor_no=couple, country=None, partner_1=partner_1, partner_2=partner_2))

    field_size = len(couples_by_round[0]) if couples_by_round else 0
    results = [
        StagingResult(
            competitor_no=entry.competitor_no,
            placement_low=final_ranks.get(entry.competitor_no),
            placement_high=final_ranks.get(entry.competitor_no),
            made_final=entry.competitor_no in final_ranks,
            field_size=field_size,
        )
        for entry in entries
    ]
    ranking = RankingPage(
        is_solo=bool(entries) and all(e.partner_2 is None for e in entries),
        rounds=staging_rounds,
        entries=entries,
        results=results,
    )
    return (
        Cre8tiveEvent(sku=sku, raw_title=raw_title, ranking=ranking, marks=marks, hesitations=hesitations, skipped_couples=skipped),
        judges_used,
    )


def officials_from(judges: dict[str, str]) -> list[StagingOfficial]:
    return [StagingOfficial(letter=jid, name=name, country=None) for jid, name in sorted(judges.items(), key=lambda kv: int(kv[0]) if kv[0].isdigit() else 0)]
