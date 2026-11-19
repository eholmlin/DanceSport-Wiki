"""Parse a Comp Manager (comp-mngr.com) heat-list page into
StagingScheduledHeat rows -- the Comp Manager counterpart of
dsr.parse.ndca.parse_heatlist_attendee, for events (e.g. Ohio Star Ball)
that publish their heat lists there instead of on NDCA Premier.

The page ("<slug>_HeatLists.htm") is one big static HTML file: a menu of
every person, and for each one a hidden block

    <div id="TABLE_CODE_21106" ...>
    <strong>Entries for Abalos, Ella</strong>
    <strong>With Blanco, Michael</strong>
    <table><tr><td>Session@Time</td><td>Number</td><td>Heat</td><td>Event</td></tr>
    <tr><td>Friday Matinee Session (7) - Regency Ballroom@11:55AM Friday</td>
        <td>433</td><td>Heat 776 [Ballroom B]</td><td>L-JR1 Open Gold International Cha Cha</td></tr>
    ...

so every couple appears twice (once under each person) and is
de-duplicated here. Things that are NOT in the page and are therefore
handled by the caller or approximated:

- Dates: rows carry only a weekday ("11:55AM Friday"); the competition's
  start date resolves it (the first such weekday on or after the start).
- Event ids: there are none, so source_event_id is "<heat number>-<slugged
  title>", which is also safe to put in a URL.
- Rounds: the bracket after the heat number names the round
  ("Semi-final Round"). A heat with no round name is a Final if no later
  rounds follow it, otherwise the first round of that many (a Semi-final
  with one later round, a Quarter-final with two).
- "Later rounds" (the callback rounds a couple only dances if recalled) are
  not loaded: whether a couple gets there isn't known yet.
- Names are "Lastname, Firstname"; they are flipped to "Firstname Lastname"
  (the form every other source uses) so people match the ones already known
  instead of creating a second copy -- see
  scripts/merge_lastname_firstname_duplicates.py for the mess that avoids.
"""
from __future__ import annotations

import datetime as dt
import re
from html import unescape

from dsr.parse.staging import StagingPersonRef, StagingScheduledHeat

SOURCE = "comp_mngr"

_BLOCK = re.compile(r'<div id="TABLE_CODE_\d+"[^>]*>(.*?)</div>', re.S)
_ROW = re.compile(r"<tr><td>(.*?)</td><td>([^<]*)</td><td>(.*?)</td><td>(.*?)</td></tr>", re.S)
_WHEN = re.compile(r"^(?P<session>.*?)@(?P<time>\d{1,2}:\d{2}[AP]M)\s+(?P<day>[A-Za-z]+day)$")
_HEAT = re.compile(r"^Heat\s+(?P<number>\S+)(?:\s*\[(?P<bracket>[^\]]*)\])?")
_ROUND_WORD = re.compile(r"(Quarter-final|Semi-final|Final)\s+Round\s*$")
_FLOOR = re.compile(r"^Ballroom\s+(\w+)")  # "Ballroom B" -> "B", the form NDCA heat lists use
_WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
_FIRST_ROUND_BY_LATER = {1: "Semi-final", 2: "Quarter-final"}


def flip_name(raw: str) -> str:
    """"Lastname, Firstname" -> "Firstname Lastname". Anything that isn't
    exactly one comma (a team, a single word) is left as written."""
    raw = " ".join(unescape(raw).split())
    if raw.count(",") == 1:
        last, first = (part.strip() for part in raw.split(","))
        if last and first:
            return f"{first} {last}"
    return raw


def _clean(cell: str) -> str:
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", cell)).split())


def _date_for_weekday(start: dt.date, weekday: str) -> dt.date | None:
    if weekday not in _WEEKDAYS:
        return None
    offset = (_WEEKDAYS.index(weekday) - start.weekday()) % 7
    return start + dt.timedelta(days=offset)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:80]


def parse_heatlists(
    html: bytes | str,
    *,
    start_date: dt.date,
    now_local: dt.datetime | None = None,
    done_after: dt.timedelta = dt.timedelta(minutes=15),
) -> list[StagingScheduledHeat]:
    """Every couple's first-round heat. `now_local` (the venue's current
    local time) marks a heat complete once it is `done_after` past its
    scheduled start -- the page has no "danced" flag of its own."""
    text = html.decode("utf-8", errors="replace") if isinstance(html, bytes) else html
    out: list[StagingScheduledHeat] = []
    seen: set[tuple] = set()
    for block in _BLOCK.findall(text):
        person_match = re.search(r"<strong>Entries for (.*?)</strong>", block, re.S)
        if not person_match:
            continue
        person = flip_name(_clean(person_match.group(1)))
        # one table per "With <partner>" heading (a person can have several
        # partners); a block with no heading is a solo entry
        sections = re.split(r"<strong>With (.*?)</strong>", block)
        pairs = [(None, sections[0])] + [(sections[i], sections[i + 1]) for i in range(1, len(sections) - 1, 2)]
        for partner_raw, chunk in pairs:
            partner = flip_name(_clean(partner_raw)) if partner_raw else None
            for when_cell, number, heat_cell, event_cell in _ROW.findall(chunk):
                heat_text = _clean(heat_cell)
                heat = _HEAT.match(heat_text)
                if heat is None:  # the header row
                    continue
                parts = re.split(r"<br\s*/?>", when_cell)
                when = _WHEN.match(_clean(parts[0]))
                if when is None:
                    continue
                later_rounds = max(0, len([p for p in parts[1:] if "@" in p]))
                event_name = _clean(event_cell)
                bracket = heat.group("bracket") or ""
                round_match = _ROUND_WORD.search(bracket)
                if round_match:
                    round_name = round_match.group(1)
                elif later_rounds == 0:
                    round_name = "Final"
                else:
                    round_name = _FIRST_ROUND_BY_LATER.get(later_rounds, "Round 1")
                floor_match = _FLOOR.match(bracket)
                day = _date_for_weekday(start_date, when.group("day"))
                scheduled = (
                    dt.datetime.combine(day, dt.datetime.strptime(when.group("time"), "%I:%M%p").time())
                    if day
                    else None
                )
                source_event_id = f"{heat.group('number')}-{_slug(event_name)}"
                couple = frozenset(n for n in (person, partner) if n)
                key = (couple, source_event_id, round_name)
                if key in seen:
                    continue
                seen.add(key)
                out.append(
                    StagingScheduledHeat(
                        source_event_id=source_event_id,
                        event_name=event_name,
                        round_name=round_name,
                        heat_number=heat.group("number"),
                        session=when.group("session").strip() or None,
                        floor=floor_match.group(1) if floor_match else None,
                        competitor_no=number.strip() or None,
                        scheduled_time=scheduled,
                        is_complete=bool(scheduled and now_local and scheduled + done_after <= now_local),
                        partner_1=StagingPersonRef(name=person, external_ref=None),
                        partner_2=StagingPersonRef(name=partner, external_ref=None) if partner else None,
                    )
                )
    return out
