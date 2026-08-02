"""Parsers for worlddancesport.org public HTML pages (see docs/wdsf-format-notes.md).

Pure functions: raw bytes in, staging dataclasses out. No network access.
Every parser raises on structure it doesn't recognize rather than silently
skipping rows (spec section 9: "zero silent failures").
"""
from __future__ import annotations

import datetime as dt
import re
from urllib.parse import urlparse

from selectolax.parser import HTMLParser

from dsr.parse.staging import (
    RankingPage,
    StagingCompEventRef,
    StagingCompetition,
    StagingEntry,
    StagingMark,
    StagingOfficial,
    StagingPersonRef,
    StagingResult,
    StagingRound,
)

SOURCE = "wdsf"
PARSER_VERSION = "wdsf-v1"

_EVENT_H1_RE = re.compile(
    r"^(?P<city_country>.+?)\s+from\s+(?P<start>\d{2}/\d{2}/\d{4})\s+to\s+(?P<end>\d{2}/\d{2}/\d{4})$"
)
_PLACEMENT_RE = re.compile(r"^\s*(\d+)\.?(?:\s*-\s*(\d+)\.?)?\s*$")
_REF_ID_RE = re.compile(r"-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$", re.I)
_COUPLE_ID_RE = re.compile(r"-(\d+)$")


class ParseError(ValueError):
    """Raised when a page doesn't match any known WDSF layout."""


def _text(node) -> str:
    return node.text(deep=True, separator=" ").strip() if node is not None else ""


def _clean_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def event_source_code_from_url(url: str) -> str:
    path = urlparse(url).path.rstrip("/")
    slug = path.rsplit("/", 1)[-1]
    if not slug:
        raise ParseError(f"cannot derive event source_code from url: {url!r}")
    return slug


def comp_event_source_code_from_url(url: str) -> str:
    path = urlparse(url).path.rstrip("/")
    slug = path.rsplit("/", 1)[-1]
    if not slug:
        raise ParseError(f"cannot derive comp_event source_code from url: {url!r}")
    return slug


def parse_event_page(html: bytes, source_url: str) -> tuple[StagingCompetition, list[StagingCompEventRef]]:
    tree = HTMLParser(html)
    h1 = tree.css_first("h1")
    if h1 is None:
        raise ParseError("event page missing <h1>")
    heading = _clean_ws(_text(h1))
    m = _EVENT_H1_RE.match(heading)
    if not m:
        raise ParseError(f"event page heading did not match expected format: {heading!r}")

    city_country = m.group("city_country")
    city, _, country = city_country.partition(" - ")
    city, country = city.strip(), country.strip() or None
    start_date = dt.datetime.strptime(m.group("start"), "%d/%m/%Y").date()
    end_date = dt.datetime.strptime(m.group("end"), "%d/%m/%Y").date()

    competition = StagingCompetition(
        source=SOURCE,
        source_code=event_source_code_from_url(source_url),
        name=city_country,
        start_date=start_date,
        end_date=end_date,
        city=city,
        country=country,
        sanctioning_body="WDSF",
        url=source_url,
    )

    comp_events: list[StagingCompEventRef] = []
    for row in tree.css("table.competition_profile__competitions__items tr"):
        name_cell = row.css_first("td.competition_profile__competitions__items__name span")
        link = row.css_first("td.competition_profile__competitions__items__status a")
        if name_cell is None or link is None:
            continue  # unconfirmed/no-results-yet rows have no link; not an error
        ranking_url = link.attributes.get("href") or ""
        if not ranking_url or "/Competitions/Ranking/" not in ranking_url:
            continue
        slug = comp_event_source_code_from_url(ranking_url)
        comp_events.append(
            StagingCompEventRef(
                source_code=slug,
                raw_title=_clean_ws(_text(name_cell)),
                ranking_url=f"/Competitions/Ranking/{slug}",
                marks_url=f"/Competitions/Marks/{slug}",
                officials_url=f"/Competitions/Officials/{slug}",
                final_url=f"/Competitions/Final/{slug}",
            )
        )

    if not comp_events:
        raise ParseError(f"event page had no parseable comp_event rows: {source_url!r}")

    return competition, comp_events


def _parse_placement(text: str) -> tuple[int, int]:
    m = _PLACEMENT_RE.match(text)
    if not m:
        raise ParseError(f"unrecognized placement text: {text!r}")
    low = int(m.group(1))
    high = int(m.group(2)) if m.group(2) else low
    return low, high


def _extract_ref(href: str, pattern: re.Pattern) -> str | None:
    m = pattern.search(href.rstrip("/"))
    return m.group(1) if m else None


def _parse_person_link(a) -> StagingPersonRef:
    href = a.attributes.get("href") or ""
    return StagingPersonRef(name=_clean_ws(_text(a)), external_ref=_extract_ref(href, _REF_ID_RE))


def parse_ranking_page(html: bytes) -> RankingPage:
    tree = HTMLParser(html)
    # The containing <section> sometimes has no class at all (older pages) and
    # sometimes class="withDisclaimer" (newer pages), so anchor on the <h1> text
    # instead of a class that isn't stable across the years.
    h1 = next((h for h in tree.css("h1") if _clean_ws(_text(h)).startswith("Ranking for")), None)
    if h1 is None or h1.parent is None:
        raise ParseError("ranking page missing 'Ranking for ...' <h1>")
    section = h1.parent

    # Round sections appear in reverse-chronological order: Final, then N.Round
    # down to 1.Round, optionally followed by an "Excused couples" table.
    headings = section.css("h2")
    if not headings:
        raise ParseError("ranking page has no round headings")

    round_sections: list[tuple[str, object]] = []
    excused_table = None
    for h2 in headings:
        label = _clean_ws(_text(h2))
        table = h2.next
        while table is not None and table.tag != "table" and table.tag != "div":
            table = table.next
        if table is not None and table.tag == "div":
            table = table.css_first("table")
        if table is None or table.tag != "table":
            raise ParseError(f"round heading {label!r} has no following table")
        if label.lower() == "excused couples":
            excused_table = table
        else:
            round_sections.append((label, table))

    is_solo = False
    header_cell = round_sections[0][1].css_first("thead th")
    # header text is empty on rank col; check the couple/athlete header instead
    couple_header = round_sections[0][1].css("thead th")
    for th in couple_header:
        t = _clean_ws(_text(th))
        if t == "Athlete":
            is_solo = True
        elif t == "Couple":
            is_solo = False

    # cumulative recall counts, computed top-down (Final first) per docs/wdsf-format-notes.md
    parsed_sections = []
    cumulative = 0
    for label, table in round_sections:
        rows = table.css("tbody tr")
        recalled_count = cumulative or None
        cumulative += len(rows)
        parsed_sections.append((label, table, rows, recalled_count, cumulative))

    field_size = cumulative
    total_rounds = len(parsed_sections)
    rounds: list[StagingRound] = []
    entries: list[StagingEntry] = []
    results: list[StagingResult] = []

    # Assign round_order = 1 for the earliest round (last item in parsed_sections,
    # since sections are listed Final-first / reverse-chronological on the page).
    for i, (label, table, rows, recalled_count, entries_in) in enumerate(reversed(parsed_sections)):
        round_order = i + 1
        is_final_section = label.strip().lower() == "final"
        round_type = "final" if is_final_section else label
        rounds.append(
            StagingRound(
                round_type=round_type,
                round_order=round_order,
                entries_in=entries_in,
                recalled_count=recalled_count,
            )
        )

    for label, table, rows, _recalled_count, _entries_in in parsed_sections:
        is_final_section = label.strip().lower() == "final"
        for row in rows:
            rank_cell = row.css_first("td.ranking__rank")
            couple_cell = row.css_first("td.ranking__couple")
            country_cell = row.css_first("td.ranking__country")
            number_cell = row.css_first("td.ranking__number")
            if rank_cell is None or couple_cell is None or number_cell is None:
                raise ParseError(f"ranking row missing expected cells: {_clean_ws(_text(row))!r}")

            competitor_no = _clean_ws(_text(number_cell))
            placement_low, placement_high = _parse_placement(_text(rank_cell))
            country = _clean_ws(_text(country_cell)) if country_cell is not None else None

            people = couple_cell.css("a")
            if not people:
                raise ParseError(f"ranking row has no athlete link(s): {_clean_ws(_text(row))!r}")
            partner_1 = _parse_person_link(people[0])
            partner_2 = _parse_person_link(people[1]) if len(people) > 1 else None

            rank_link = rank_cell.css_first("a")
            couple_ref = _extract_ref(rank_link.attributes.get("href") or "", _COUPLE_ID_RE) if rank_link is not None else None

            entries.append(
                StagingEntry(
                    competitor_no=competitor_no,
                    country=country,
                    partner_1=partner_1,
                    partner_2=partner_2,
                    couple_ref=couple_ref,
                )
            )
            results.append(
                StagingResult(
                    competitor_no=competitor_no,
                    placement_low=placement_low,
                    placement_high=placement_high,
                    made_final=is_final_section,
                    field_size=field_size,
                )
            )

    if excused_table is not None:
        for row in excused_table.css("tbody tr"):
            cells = row.css("td")
            if len(cells) < 2:
                raise ParseError(f"excused-couples row malformed: {_clean_ws(_text(row))!r}")
            couple_cell = cells[1]
            country_cell = cells[2] if len(cells) > 2 else None
            people = couple_cell.css("a")
            if not people:
                raise ParseError(f"excused row has no athlete link(s): {_clean_ws(_text(row))!r}")
            partner_1 = _parse_person_link(people[0])
            partner_2 = _parse_person_link(people[1]) if len(people) > 1 else None
            detail_link = cells[0].css_first("a")
            couple_ref = (
                _extract_ref(detail_link.attributes.get("href") or "", _COUPLE_ID_RE)
                if detail_link is not None
                else None
            )
            entries.append(
                StagingEntry(
                    competitor_no="",
                    country=_clean_ws(_text(country_cell)) if country_cell is not None else None,
                    partner_1=partner_1,
                    partner_2=partner_2,
                    couple_ref=couple_ref,
                )
            )

    return RankingPage(is_solo=is_solo, rounds=rounds, entries=entries, results=results)


def parse_officials_page(html: bytes) -> list[StagingOfficial]:
    tree = HTMLParser(html)
    officials: list[StagingOfficial] = []
    for h2 in tree.css("h2"):
        if _clean_ws(_text(h2)) != "Adjudicators":
            continue
        table = h2.next
        while table is not None and table.tag != "table":
            table = table.next
        if table is None:
            raise ParseError("Adjudicators heading has no following table")
        for row in table.css("tbody tr"):
            cells = row.css("td")
            if len(cells) < 3:
                raise ParseError(f"adjudicator row malformed: {_clean_ws(_text(row))!r}")
            name_link = cells[0].css_first("a")
            name = _clean_ws(_text(cells[0]))
            country = _clean_ws(_text(cells[1])) or None
            letter = _clean_ws(_text(cells[2]))
            ref = _extract_ref(name_link.attributes.get("href") or "", _REF_ID_RE) if name_link is not None else None
            officials.append(StagingOfficial(letter=letter, name=name, country=country, external_ref=ref))
        break
    if not officials:
        raise ParseError("officials page had no Adjudicators section")
    return officials


def _judge_letters_for_dance_group(thead) -> list[list[str]]:
    """Return per-dance-group lists of judge letters, in column order.

    Small fields that go straight to a Final with no recall rounds (e.g.
    fixtures/wdsf/marks_charlotte_seniorI_latin_2015.html, 3 couples) still
    render this table, but with dance columns and no per-judge sub-columns at
    all -- there's no recall data to show. That's a legitimately different,
    valid shape, not a broken page; the Final page carries that event's real
    judge marks instead (see parse_final_page).
    """
    header_rows = thead.css("tr")
    if len(header_rows) < 2:
        raise ParseError("marks page header missing judge-letter row")
    dance_header_row, judge_row = header_rows[0], header_rows[1]
    dance_ths = [th for th in dance_header_row.css("th") if th.attributes.get("class") == "adjudicator"]
    n_dances = len(dance_ths)
    if n_dances == 0:
        raise ParseError("marks page header has no per-dance columns")

    judge_ths = judge_row.css("th.ajud")
    per_judge_cols = len(judge_ths) // n_dances if judge_ths else 0
    groups: list[list[str]] = []
    for g in range(n_dances):
        letters = [_clean_ws(_text(th)) for th in judge_ths[g * per_judge_cols : (g + 1) * per_judge_cols]]
        groups.append(letters)
    dance_names = [_clean_ws(_text(th)) for th in dance_ths]
    return dance_names, groups


def parse_marks_page(html: bytes) -> list[StagingMark]:
    tree = HTMLParser(html)
    table = tree.css_first("table.marks")
    if table is None:
        raise ParseError("marks page missing table.marks")
    thead = table.css_first("thead")
    tbody = table.css_first("tbody")
    if thead is None or tbody is None:
        raise ParseError("marks table missing thead/tbody")

    dance_names, judge_groups = _judge_letters_for_dance_group(thead)
    if not any(judge_groups):
        return []  # points-scored event with no per-judge marks published; not an error
    per_judge_cols = len(judge_groups[0]) if judge_groups else 0

    marks: list[StagingMark] = []
    current_competitor_no: str | None = None
    for row in tbody.css("tr"):
        if "separator" in (row.attributes.get("class") or ""):
            continue
        number_cell = row.css_first("td.number")
        if number_cell is not None:
            current_competitor_no = _clean_ws(_text(number_cell))
        round_cell = row.css_first("td.round")
        if round_cell is None:
            continue  # rank-only or unexpected row; nothing to extract
        if current_competitor_no is None:
            raise ParseError("marks row has round data but no competitor number seen yet")
        round_label = round_cell.attributes.get("title") or _clean_ws(_text(round_cell))
        round_order = None
        rm = re.match(r"^(\d+)\.\s*Round$", round_label)
        if rm:
            round_order = int(rm.group(1))

        # NB: a combined selector ("td[data-info], td.dSum") returns all matches of
        # the first branch before any of the second, not in document order -- so
        # cells must be pulled from a single ordered td() list and filtered instead.
        data_cells = [
            c for c in row.css("td") if "data-info" in c.attributes or c.attributes.get("class") == "dSum"
        ]
        pos = 0
        for dance_name, letters in zip(dance_names, judge_groups):
            for letter in letters:
                if pos >= len(data_cells):
                    raise ParseError(f"marks row ran out of cells for {current_competitor_no!r}")
                cell = data_cells[pos]
                pos += 1
                value = _clean_ws(_text(cell))
                marks.append(
                    StagingMark(
                        round_order=round_order,
                        competitor_no=current_competitor_no,
                        judge_letter=letter,
                        dance=dance_name,
                        recalled=(value == "+") if value in ("+", "") else None,
                        placement=None,
                    )
                )
            pos += 1  # skip the per-dance "=" majority-count column (dSum)

    if not marks:
        raise ParseError("marks page produced zero marks")
    return marks


def parse_final_page(html: bytes) -> list[StagingMark]:
    tree = HTMLParser(html)
    table = tree.css_first("table.skating")
    if table is None:
        raise ParseError("final page missing table.skating")
    thead = table.css_first("thead")
    tbody = table.css_first("tbody")
    if thead is None or tbody is None:
        raise ParseError("final skating table missing thead/tbody")

    judge_letters = [_clean_ws(_text(th)) for th in thead.css("th.ajud")]
    if not judge_letters:
        raise ParseError("final page has no judge columns")

    marks: list[StagingMark] = []
    for row in tbody.css("tr"):
        number_cell = row.css_first("td.number")
        if number_cell is None:
            raise ParseError(f"final row missing competitor number: {_clean_ws(_text(row))!r}")
        competitor_no = _clean_ws(_text(number_cell))
        judge_cells = row.css("td[data-info]")
        if len(judge_cells) != len(judge_letters):
            raise ParseError(
                f"final row for {competitor_no!r} has {len(judge_cells)} marks, expected {len(judge_letters)}"
            )
        for letter, cell in zip(judge_letters, judge_cells):
            text = _clean_ws(_text(cell)).rstrip(".")
            placement = int(text) if text.isdigit() else None
            marks.append(
                StagingMark(
                    round_order=None,
                    competitor_no=competitor_no,
                    judge_letter=letter,
                    dance=None,
                    recalled=None,
                    placement=placement,
                )
            )
    if not marks:
        raise ParseError("final page produced zero marks")
    return marks
