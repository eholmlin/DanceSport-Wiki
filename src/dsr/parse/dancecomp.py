"""Parser for DanceComp (dancecomp.io), a modern results viewer built on
top of Comp Manager data for some competitions (Holiday Dance Classic
confirmed; the URL path literally reads .../api/compmgr/...).

Unlike comp-mngr.com's own pipe-delimited .dat export, dancecomp.io
server-renders its results page with the full dataset embedded as a JSON
array literal inside a `<script>` tag (a DataTables `data: [...]`
initializer) -- no separate API call or JS execution needed, just a
regex extraction of that array followed by json.loads. Confirmed via a
real fixture (Holiday Dance Classic 2023, comp_code "HC2023", comp_id
124): https://dancecomp.io/api/compmgr/result/HC2023/124 returned
10,042 rows across 4,135 distinct heats in one response, no pagination.

Real, inherent limitation of this source: it exposes per-heat *outcomes*
(a numeric final placement, or "SF"/"QF" marking the furthest round a
couple reached without a numeric rank), not per-judge marks -- there is
no equivalent of comp-mngr.com's judge-by-judge "R"/placement columns
anywhere in this endpoint. Every DancecompEventData below carries an
empty marks list; only StagingResult data is produced.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from dsr.parse.staging import RankingPage, StagingEntry, StagingPersonRef, StagingResult, StagingRound

SOURCE = "dancecomp"
PARSER_VERSION = "dancecomp-v1"

# Same rationale as dsr.parse.comp_mngr._ROUND_RANK: relative order only,
# "final has the highest round_order" is the one invariant callers rely on.
_RECALL_ROUND_SUFFIXES = ("Quarter-final", "Semi-final")
_ROUND_RANK = {"Quarter-final": 1, "Semi-final": 2, "Final": 3}


class ParseError(ValueError):
    """Raised when the page doesn't match the expected shape."""


@dataclass
class DancecompEventData:
    raw_title: str
    ranking: RankingPage
    marks: list = None  # always empty -- see module docstring

    def __post_init__(self):
        if self.marks is None:
            self.marks = []


def _extract_data_array(html: bytes) -> list[dict]:
    text = html.decode("utf-8", errors="replace")
    match = re.search(r"data:\s*(\[.*?\])\s*,\s*columns", text, re.S)
    if not match:
        raise ParseError("no DataTables 'data: [...]' array found in page")
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise ParseError(f"embedded data array is not valid JSON: {exc}") from exc


def _strip_heat_number(heat_title: str) -> str:
    # "Heat 391: L-M Pro/Am ... (C/R/SW) - Quarter-final" -> drop the
    # "Heat 391: " prefix. Heat numbers are assigned by schedule position,
    # not by division -- the same division's prelim/quarter/semi/final
    # rounds run at different points in the day and get different heat
    # numbers, so keeping the prefix would fracture one division's rounds
    # into unrelated CompEvents (same reasoning as comp_mngr's raw_title,
    # which strips its own "Heat N:" prefix the same way).
    if heat_title.startswith("Heat "):
        return heat_title.split(":", 1)[1].strip()
    return heat_title


def _strip_round_suffix(heat_title: str) -> tuple[str, str]:
    heat_title = _strip_heat_number(heat_title)
    for suffix in _RECALL_ROUND_SUFFIXES:
        marker = f" - {suffix}"
        if heat_title.endswith(marker):
            return heat_title[: -len(marker)], suffix
    if heat_title.endswith(" Final"):
        # Divisions with a real recall structure (a Quarter-final/
        # Semi-final sibling elsewhere) title their final round
        # "<base title> Final" -- no dash, unlike the recall rounds'
        # " - Semi-final" marker. Stripping this too is what lets a
        # division's recall and final rounds share one raw_title and
        # merge into a single CompEvent, same as comp_mngr. Single-round
        # divisions with no recall concept at all (e.g. "Proficiency"
        # heats) never carry this suffix and pass through unchanged.
        return heat_title[: -len(" Final")], "Final"
    return heat_title, "Final"


def _split_couple_name(name: str) -> tuple[StagingPersonRef, StagingPersonRef | None]:
    # " - " (space-dash-space) is the couple separator -- confirmed safe
    # against hyphenated given/family names (e.g. "Anna-Marie"), which
    # never carry surrounding spaces around their internal hyphen.
    parts = [p.strip() for p in name.split(" - ", 1)]
    partner_1 = StagingPersonRef(name=parts[0])
    partner_2 = StagingPersonRef(name=parts[1]) if len(parts) > 1 and parts[1] else None
    return partner_1, partner_2


def parse_result(result_html: bytes) -> tuple[list[DancecompEventData], int]:
    """Parse a `/api/compmgr/result/<comp_code>/<comp_id>` response into
    one DancecompEventData per (event, round). Multiple rounds of the
    same event arrive as separate DancecompEventData sharing one
    raw_title, same merging convention as dsr.parse.comp_mngr.

    Returns (events, rows_skipped) -- rows_skipped counts rows that
    couldn't be matched to a bib/name pattern (logged by the caller, not
    fatal), same resilience pattern as the rest of this project's
    parsers.
    """
    rows = _extract_data_array(result_html)

    # Group by (raw_title, round_type) first so field_size (how many
    # couples competed in that specific round) is correct.
    groups: dict[tuple[str, str], list[dict]] = {}
    skipped = 0
    for row in rows:
        heat = row.get("heat")
        name = row.get("name")
        bib = row.get("number_on_the_back")
        if not heat or not name or not bib:
            skipped += 1
            continue
        raw_title, round_type = _strip_round_suffix(heat)
        groups.setdefault((raw_title, round_type), []).append(row)

    events: list[DancecompEventData] = []
    for (raw_title, round_type), group_rows in groups.items():
        field_size = len(group_rows)
        entries: list[StagingEntry] = []
        results: list[StagingResult] = []
        for row in group_rows:
            partner_1, partner_2 = _split_couple_name(row["name"])
            bib = str(row["number_on_the_back"])
            entries.append(StagingEntry(competitor_no=bib, country=None, partner_1=partner_1, partner_2=partner_2))

            place = row.get("place")
            if isinstance(place, (int, float)):
                placement = int(place)
                results.append(
                    StagingResult(
                        competitor_no=bib, placement_low=placement, placement_high=placement,
                        made_final=True, field_size=field_size,
                    )
                )
            else:
                # place is "SF"/"QF" (or some other non-numeric marker):
                # the couple's furthest reached round, no numeric rank --
                # recorded as a made_final=False result (present, no
                # placement) rather than dropped, same as a source's
                # "not recalled" case elsewhere in this project.
                results.append(
                    StagingResult(
                        competitor_no=bib, placement_low=None, placement_high=None,
                        made_final=False, field_size=field_size,
                    )
                )

        staging_round = StagingRound(
            round_type=round_type, round_order=_ROUND_RANK[round_type], entries_in=field_size, recalled_count=None
        )
        ranking = RankingPage(is_solo=False, rounds=[staging_round], entries=entries, results=results)
        events.append(DancecompEventData(raw_title=raw_title, ranking=ranking))

    return events, skipped
