"""Parser for Comp Manager (comp-mngr.com) event exports.

Comp Manager is a third-party results platform some NDCA-sanctioned
competitions use instead of (or before switching to) NDCA Premier -- see
docs/comp-mngr-format-notes.md. Confirmed via real fixtures (Tri-State
DanceSport Championships 2023, Wisconsin State Dance Championships 2023):
each event directory (e.g. comp-mngr.com/tristate2023/) exposes a
`<slug>_scoresheetsbyperson.dat` file that is NOT the per-dance table its
name suggests -- it's the single most complete data source on the site,
containing every heat's judge-by-judge marks (recall rounds) or skated
placements (final rounds), plus multi-dance combined-event summaries.
The paired `<Slug>_ScoresheetsByPerson.htm` page carries the competition
name, the judge-number-to-name legend, and the full person directory
(id -> "Lastname, Firstname (bib)").

Three block shapes appear in the .dat file, delimited by a `<id,id,...`
header line and a bare `>` terminator line:

1. Regular per-dance heat (the vast majority -- ~85-90% of blocks):
   `Heat N: <event title>[ - Semi-final|Quarter-final]`, then a
   `|No.|<judge nos>|...|<result cols>|` header and one row per couple.
   No suffix means the final/skated round -- see _parse_heat_block.

2. `=Combined Event: Combined event award for: <event title>` -- a
   derived overall-placement summary for a multi-dance championship whose
   individual dances were recorded as separate regular heat blocks
   elsewhere. No judge marks here, just a placement -- becomes a
   StagingResult with no accompanying StagingMark rows.

3. `=Heat N: <event title>` (no "Combined Event" prefix) -- a *self-
   contained* multi-dance final: one sub-table per dance followed by a
   "Final summary" sub-table, all inside a single block (used when the
   field was small enough to skip recall rounds entirely). Not handled by
   v1 -- logged and skipped, same resilience pattern as
   dsr.parse.ndca's "one malformed event must not lose its siblings".

`Solo N: ...` blocks (individual showcase routines, scored by average
rather than placement/recall) are a fourth, distinct shape -- also
skipped in v1.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

from dsr.parse.staging import (
    RankingPage,
    StagingCompetition,
    StagingEntry,
    StagingMark,
    StagingOfficial,
    StagingPersonRef,
    StagingResult,
    StagingRound,
)

SOURCE = "comp_mngr"
PARSER_VERSION = "comp-mngr-v1"

# Suffixes stripped from a heat title to recover the round type; a title
# with none of these is the final (skated) round. Order doesn't matter --
# titles carry at most one.
_RECALL_ROUND_SUFFIXES = ("Quarter-final", "Semi-final")
# Relative order only (see load/wdsf.py's _get_or_create_round_by_label:
# "final has the highest round_order" is the one invariant callers rely
# on) -- not every event has every round, and that's fine, since these
# are never required to be contiguous.
_ROUND_RANK = {"Quarter-final": 1, "Semi-final": 2, "final": 3}


class ParseError(ValueError):
    """Raised when a required page doesn't match the expected shape."""


@dataclass
class CompMngrEventData:
    raw_title: str
    ranking: RankingPage
    marks: list[StagingMark]


def _decode(raw: bytes) -> str:
    # Source files use Windows CRLF line endings -- normalized here so no
    # downstream line ever carries a stray trailing \r. Real bug this
    # fixes: a block's own `<id,id,...` header line's *last* id kept its
    # \r (every earlier id in the list was protected by the comma after
    # it), silently failing to match that one person in the directory
    # while everyone else in the same block matched fine.
    return raw.decode("utf-8", errors="replace").replace("\r\n", "\n")


def parse_judges(index_html: bytes) -> list[StagingOfficial]:
    """Parse the "List of Judges" legend on a ScoresheetsByPerson index
    page: lines like "04 Didio Barrera<br>" map judge number -> name.
    The .dat file's mark columns are numbers, not names, so this is the
    only place judge identity is recoverable."""
    text = _decode(index_html)
    marker = text.find("List of Judges")
    if marker == -1:
        raise ParseError("no 'List of Judges' section found in index page")
    tail = text[marker:]
    officials = []
    for match in re.finditer(r"(\d{2})\s+([^<\n]+?)\s*<br>", tail):
        number, name = match.group(1), match.group(2).strip()
        officials.append(StagingOfficial(letter=number, name=name, country=None, external_ref=None))
    if not officials:
        raise ParseError("'List of Judges' section had no parseable entries")
    return officials


def parse_competition_name(index_html: bytes) -> str:
    """The competition name appears in a hidden form field
    (`COMP_NAME`) on the ScoresheetsByPerson index page -- more reliable
    than the decorative <font> header, which sometimes carries extra
    banner text (e.g. "**Heat Lists are Tentative...**")."""
    text = _decode(index_html)
    match = re.search(r'name="COMP_NAME"\s+value="([^"]+)"', text)
    if not match:
        raise ParseError("no COMP_NAME field found in index page")
    return match.group(1).strip()


_DATE_CLUSTER_WINDOW = dt.timedelta(days=10)


def parse_directory_listing_dates(listing_html: bytes) -> tuple[dt.date | None, dt.date | None]:
    """Comp Manager exposes no explicit competition-date field anywhere
    in its own pages, so dates are inferred from the Apache "Index of
    /<slug>/" directory listing's file modification timestamps instead.
    Two categories of file are excluded outright, both confirmed on real
    fixtures to carry dates unrelated to the actual competition:

    1. Non-.htm/.dat files entirely -- a real case (Wisconsin State
    2023) has dozens of per-studio `.efo` *registration* submissions
    (e.g. "wisconsin2023_studio303wigmailcom.efo") timestamped anywhere
    from 7 weeks before the event to well after it, plus a `.ZPA`
    project-archive file (United States Dance Championships 2023) that
    was resaved a full YEAR later (2024-07-31). Neither reflects when
    the competition itself happened.

    2. The ScoresheetsByPerson page/.dat file specifically, even though
    they ARE .htm/.dat -- confirmed on New York Dance Festival 2023 that
    this pair alone can be regenerated long after the fact (stamped
    2023-04-12, two months after HeatLists.htm/Heats.htm's
    2023-02-16/02-26 and the competition's real Feb 23 2023 date, found
    via web search when this source was discovered).

    Even after those two exclusions, some competitions still have other
    plain .htm files mass-resaved long after the event -- Holiday Dance
    Classic 2023's CategoryResults.htm/Placements.htm/40+
    ResultsCategory_*.htm files are all stamped 2024-07-30, seven months
    after the real 2023-12-10 event; its 2025 edition has three
    different stale clusters (Placements/ScoresheetsByPerson resaved a
    month later, ResultsCategory_* two weeks later). Neither is caught
    by the two exclusions above. HeatLists.htm/Heats.htm is the one file
    confirmed to land at the true event date in every competition
    checked, including these -- so it's used as an anchor: any other
    file whose date falls within _DATE_CLUSTER_WINDOW of it is trusted,
    everything further out is treated as a stale resave and dropped.
    When no HeatLists/Heats file is present (not observed yet, but
    possible), falls back to the plain min/max of every included file.

    Returns (None, None) if no qualifying rows are found, rather than
    raising, since this is best-effort enrichment, not required for a
    usable competition row."""
    text = _decode(listing_html)
    rows = re.findall(r'href="([^"]+)"[^<]*<\/a>[^<]*<\/td><td[^>]*>(\d{4}-\d{2}-\d{2})\s+\d{2}:\d{2}', text)
    href_dates = [
        (href, dt.date.fromisoformat(date_str))
        for href, date_str in rows
        if href.lower().endswith((".htm", ".dat")) and "scoresheetsbyperson" not in href.lower()
    ]
    if not href_dates:
        return None, None

    anchor = next((date for href, date in href_dates if "heatlist" in href.lower()), None)
    if anchor is None:
        dates = sorted(date for _href, date in href_dates)
        return dates[0], dates[-1]

    clustered = sorted(date for _href, date in href_dates if abs(date - anchor) <= _DATE_CLUSTER_WINDOW)
    return clustered[0], clustered[-1]


def parse_competition(
    index_html: bytes, listing_html: bytes | None, *, source_code: str, url: str
) -> StagingCompetition:
    """Assemble a StagingCompetition from the index page (name) and,
    optionally, the directory listing (dates -- see
    parse_directory_listing_dates). `sanctioning_body` is hardcoded to
    "NDCA": every Comp Manager competition sampled during discovery was
    confirmed NDCA-sanctioned (that's exactly why it's a gap in NDCA
    Premier-only scraping -- the event runs under NDCA but published on
    a different platform some years), and comp-mngr.com's own pages
    never state this explicitly, so there's no per-competition field to
    read it from."""
    name = parse_competition_name(index_html)
    start_date, end_date = parse_directory_listing_dates(listing_html) if listing_html is not None else (None, None)
    return StagingCompetition(
        source=SOURCE,
        source_code=source_code,
        name=name,
        start_date=start_date,
        end_date=end_date,
        city=None,
        country="USA",
        sanctioning_body="NDCA",
        url=url,
    )


def parse_person_directory(index_html: bytes) -> dict[str, StagingPersonRef]:
    """Parse the `<option value='ID=Lastname, Firstname (bib)'>` person
    picker into {person_id: PersonRef}, one entry per person regardless
    of whether their option carries a bib -- every person is kept,
    since a block's own person-id list (see _resolve_couple_names) is
    what's actually used to look these up, not the bib. The trailing
    " (bib)" is stripped from the name itself: confirmed only one
    partner in a couple gets it annotated here (real case: "Ablitsova,
    Olena (191)" but her partner is just "Reyzin, Zoe", no bib at all),
    so leaving it in would make the same person's name inconsistent
    across competitions/entries and hurt entity-resolution matching."""
    text = _decode(index_html)
    persons: dict[str, StagingPersonRef] = {}
    for match in re.finditer(r"<option value='(\d+)=([^']+)'>", text):
        person_id, label = match.group(1), match.group(2).strip()
        name = re.sub(r"\s*\(\d+\)$", "", label)
        persons[person_id] = StagingPersonRef(name=name, external_ref=None)
    return persons


def _split_blocks(dat_text: str) -> list[list[str]]:
    """Split the .dat file on its `<id,id,...` ... `>` block delimiters.
    `=`-prefixed content lines (e.g. "=Combined Event: ...") are NOT
    block starts -- confirmed against real fixtures: every block's true
    first line always begins with "<"; a second line starting with "="
    just marks that block's *content* as a combined-event/multi-dance
    shape rather than a regular heat."""
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in dat_text.split("\n"):
        if line.startswith("<"):
            if current:
                blocks.append(current)
            current = [line]
        elif line == ">":
            if current:
                current.append(line)
                blocks.append(current)
                current = []
        elif current:
            current.append(line)
    return blocks


def _strip_round_suffix(heat_title: str) -> tuple[str, str]:
    for suffix in _RECALL_ROUND_SUFFIXES:
        marker = f" - {suffix}"
        if heat_title.endswith(marker):
            return heat_title[: -len(marker)], suffix
    return heat_title, "final"


def _parse_table_rows(lines: list[str]) -> tuple[list[str], list[list[str]]]:
    """The first `|...|` line is the column header; every following
    `|...|` line is a data row, both split on `|` with the leading/
    trailing empty strings (from the outer pipes) dropped."""
    header: list[str] | None = None
    rows: list[list[str]] = []
    for line in lines:
        if not line.startswith("|"):
            continue
        cells = line.split("|")[1:-1]
        if header is None:
            header = cells
        else:
            rows.append(cells)
    if header is None:
        raise ParseError("table has no header row")
    return header, rows


_ENTRY_ROW = re.compile(r"^(\d+)\s+(.*)$")
_BLOCK_ID_LIST = re.compile(r"^<([\d,]+)$")


def _resolve_couple_names(
    abbrev_names: list[str], candidate_ids: list[str], directory: dict[str, StagingPersonRef], claimed: set[str]
) -> list[str]:
    """The .dat file's own rows only carry last names (e.g. "Fantauzzi/
    D'Arpino"); full "Firstname Lastname" pairs have to be recovered by
    matching each abbreviated surname against the block's own person-id
    list (its `<id,id,...` header line) looked up in the person
    directory parsed from the index page. Necessary because that
    directory itself is incomplete per-person -- confirmed on a real
    fixture: a bib number is annotated on only ONE partner's entry
    ("Ablitsova, Olena (191)"), never both, so bib lookup alone can't
    recover the other partner's ("Reyzin, Zoe") full name. Matching is
    scoped to just this block's small id pool (not the whole
    competition), where a same-surname collision is unlikely; `claimed`
    prevents the same id being matched twice within one row. Falls back
    to the bare abbreviated surname when no confident match is found,
    rather than failing the whole row.
    """
    resolved = []
    for surname in abbrev_names:
        match = next(
            (
                pid
                for pid in candidate_ids
                if pid not in claimed
                and pid in directory
                and directory[pid].name.split(",")[0].strip().lower() == surname.lower()
            ),
            None,
        )
        if match is not None:
            claimed.add(match)
            resolved.append(directory[match].name)
        else:
            resolved.append(surname)
    return resolved


def _parse_heat_block(block: list[str], directory: dict[str, StagingPersonRef]) -> CompMngrEventData | None:
    """Parse one regular `Heat N: <title>[ - <round>]` block into a
    single-round CompMngrEventData, or None for a shape v1 doesn't
    handle (multi-dance-in-one-block finals, solos)."""
    header_line = block[1]
    if not header_line.startswith("Heat "):
        return None  # "=Combined Event"/"=Heat"/"Solo " -- handled elsewhere or skipped
    title_part = header_line.split(":", 1)[1].strip()
    raw_title, round_type = _strip_round_suffix(title_part)

    id_match = _BLOCK_ID_LIST.match(block[0])
    candidate_ids = id_match.group(1).split(",") if id_match else []

    header, rows = _parse_table_rows(block[2:])
    is_recall_round = "Recall" in header
    # Judge columns are every 2-digit numeral immediately after "No." --
    # taking while matching (not filtering the whole header) matters for
    # skated rounds: `|No.|04|13|...|24||1|1-2|Result|` has an empty cell
    # then single-digit threshold-vote columns ("1", "1-2", ...) right
    # after the judges, and a bare filter would wrongly count that lone
    # "1" threshold column as a 2-digit-less judge number too.
    judge_numbers: list[str] = []
    for cell in header[1:]:
        if re.fullmatch(r"\d{2}", cell):
            judge_numbers.append(cell)
        else:
            break

    entries: list[StagingEntry] = []
    results: list[StagingResult] = []
    marks: list[StagingMark] = []
    field_size = len(rows)
    recalled_count = 0

    for row in rows:
        entry_match = _ENTRY_ROW.match(row[0].strip())
        if not entry_match:
            continue  # a bye/excused row with no bib
        bib, names_part = entry_match.group(1), entry_match.group(2)
        # A blank second name (e.g. "768 Frecautan/") is a real source
        # convention, not a parsing artifact -- confirmed on New York
        # Dance Festival 2023: the professional half of a Pro-Am couple
        # is sometimes omitted entirely from this abbreviated form, with
        # nothing recoverable about who they are from this row. Filtered
        # out here rather than kept as an empty-string StagingPersonRef:
        # every blank name would otherwise resolve to person_id via exact
        # name matching and silently merge every such instructor -- 91
        # different real professionals collapsed onto one Person row was
        # a real bug this caught.
        abbrev_names = [n.strip() for n in names_part.split("/", 1) if n.strip()]
        claimed: set[str] = set()
        name_parts = _resolve_couple_names(abbrev_names, candidate_ids, directory, claimed)
        partner_1 = StagingPersonRef(name=name_parts[0])
        partner_2 = StagingPersonRef(name=name_parts[1]) if len(name_parts) > 1 else None
        entries.append(StagingEntry(competitor_no=bib, country=None, partner_1=partner_1, partner_2=partner_2))

        judge_cells = row[1 : 1 + len(judge_numbers)]

        if is_recall_round:
            recalled = row[-1].strip() == "Recall"
            if recalled:
                recalled_count += 1
            for judge_no, cell in zip(judge_numbers, judge_cells):
                marks.append(
                    StagingMark(
                        round_label=round_type,
                        competitor_no=bib,
                        judge_letter=judge_no,
                        dance=None,
                        recalled=(cell.strip() == "R"),
                        placement=None,
                    )
                )
        else:
            result_cell = row[-1].strip()
            placement = int(float(result_cell)) if result_cell and result_cell.replace(".", "", 1).isdigit() else None
            if placement is not None:
                results.append(
                    StagingResult(
                        competitor_no=bib,
                        placement_low=placement,
                        placement_high=placement,
                        made_final=True,
                        field_size=field_size,
                    )
                )
            for judge_no, cell in zip(judge_numbers, judge_cells):
                cell = cell.strip()
                mark_placement = int(float(cell)) if cell and cell.replace(".", "", 1).isdigit() else None
                marks.append(
                    StagingMark(
                        round_label=round_type,
                        competitor_no=bib,
                        judge_letter=judge_no,
                        dance=None,
                        recalled=None,
                        placement=mark_placement,
                    )
                )

    staging_round = StagingRound(
        round_type=round_type,
        round_order=_ROUND_RANK[round_type],
        entries_in=field_size,
        recalled_count=recalled_count if is_recall_round else None,
    )
    ranking = RankingPage(is_solo=False, rounds=[staging_round], entries=entries, results=results)
    return CompMngrEventData(raw_title=raw_title, ranking=ranking, marks=marks)


def _parse_combined_event_block(block: list[str]) -> CompMngrEventData | None:
    """`=Combined Event: Combined event award for: <title>` -- a derived
    overall placement across dances already recorded as separate regular
    heat blocks. No per-judge marks here (the per-dance columns are each
    dance's *placement*, not a judge's mark), so this produces a
    Result-only CompMngrEventData with an empty marks list."""
    header_line = block[1]
    if not header_line.startswith("=Combined Event: Combined event award for:"):
        return None
    raw_title = header_line.split(":", 2)[-1].strip()

    header, rows = _parse_table_rows(block[2:])
    entries: list[StagingEntry] = []
    results: list[StagingResult] = []
    field_size = len(rows)

    for row in rows:
        entry_match = _ENTRY_ROW.match(row[0].strip())
        if not entry_match:
            continue
        bib, names_part = entry_match.group(1), entry_match.group(2)
        # Same defensive filter as _parse_heat_block -- a blank second
        # name is a real (if rare) source omission, not a name to keep.
        name_parts = [n.strip() for n in re.split(r"\s*/\s*", names_part, maxsplit=1) if n.strip()]
        partner_1 = StagingPersonRef(name=name_parts[0])
        partner_2 = StagingPersonRef(name=name_parts[1]) if len(name_parts) > 1 else None
        entries.append(StagingEntry(competitor_no=bib, country=None, partner_1=partner_1, partner_2=partner_2))

        place_cell = row[-1].strip()
        placement = int(float(place_cell)) if place_cell and place_cell.replace(".", "", 1).isdigit() else None
        if placement is not None:
            results.append(
                StagingResult(
                    competitor_no=bib,
                    placement_low=placement,
                    placement_high=placement,
                    made_final=True,
                    field_size=field_size,
                )
            )

    staging_round = StagingRound(round_type="final", round_order=1, entries_in=field_size, recalled_count=None)
    ranking = RankingPage(is_solo=False, rounds=[staging_round], entries=entries, results=results)
    return CompMngrEventData(raw_title=raw_title, ranking=ranking, marks=[])


def parse_scoresheets_dat(
    dat_bytes: bytes, directory: dict[str, StagingPersonRef] | None = None
) -> tuple[list[CompMngrEventData], int]:
    """Parse a `<slug>_scoresheetsbyperson.dat` file into one
    CompMngrEventData per (event, round) block. Multiple rounds of the
    same event arrive as separate CompMngrEventData with the same
    raw_title -- callers merge them the way dsr.load.wdsf.load_ranking_page
    already merges any StagingRound list sharing a comp_event (each
    _load_round call is independent, keyed by round_type).

    `directory` (from parse_person_directory) recovers full names for
    regular heat blocks, which otherwise only carry last names -- see
    _resolve_couple_names. Optional because "Combined Event" blocks
    already carry full names natively and don't need it; omitting it
    for a file with regular heat blocks just falls back to last-name-only
    StagingPersonRefs (still usable, just lower entity-resolution quality).

    Returns (events, skipped_count) -- skipped_count covers block shapes
    v1 doesn't parse yet (multi-dance-in-one-block finals, solos), logged
    by the caller rather than raised, matching dsr.parse.ndca's "one
    malformed event must not lose every other event" resilience pattern.
    """
    text = _decode(dat_bytes)
    blocks = _split_blocks(text)
    events: list[CompMngrEventData] = []
    skipped = 0
    for block in blocks:
        if len(block) < 2:
            skipped += 1
            continue
        parsed = _parse_heat_block(block, directory or {}) or _parse_combined_event_block(block)
        if parsed is None:
            skipped += 1
            continue
        events.append(parsed)
    return events, skipped
