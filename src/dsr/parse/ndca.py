"""Parser for the NDCA Premier JSON results feed (see docs/ndca-format-notes.md).

Unlike WDSF, one JSON blob already contains everything for an event (rounds,
dances, judges, marks, placements) -- no separate ranking/marks/officials/
final pages to fetch and correlate. Both feed shapes
(`?cyi=X&id=<competitor>` -> {"Events": [...]}, `?cyi=X&event=<id>` -> one
event dict) funnel through the same per-event parser.

Per docs/ndca-format-notes.md: NDCA's competitor/judge ids are NOT treated as
stable external ids the way WDSF's athlete GUIDs are (no evidence they're
globally stable across competitions) -- StagingPersonRef.external_ref is
deliberately left unset here, so resolve_person always falls through to
name-based (exact alias, then fuzzy) matching for this source.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

from dsr.parse.staging import (
    RankingPage,
    StagingCompetition,
    StagingEntry,
    StagingMark,
    StagingOfficial,
    StagingPersonRef,
    StagingResult,
    StagingRound,
    StagingScheduledHeat,
)

SOURCE = "ndca_premier"
PARSER_VERSION = "ndca-v1"


class ParseError(ValueError):
    """Raised when the feed doesn't match the expected shape."""


@dataclass
class NdcaEventData:
    source_code: str  # NDCA numeric event id, as a string
    raw_title: str
    ranking: RankingPage
    officials: list[StagingOfficial]
    marks: list[StagingMark]


def _require(d: dict, key: str, context: str):
    if key not in d:
        raise ParseError(f"missing {key!r} in {context}")
    return d[key]


def _parse_publish_timestamp(s: str | None) -> dt.datetime | None:
    """NDCA's Publish_Dates values look like '2026-02-08 11:44:13 am'."""
    return dt.datetime.strptime(s, "%Y-%m-%d %I:%M:%S %p") if s else None


def _competition_from_compyears_entry(e: dict) -> StagingCompetition:
    def parse_date(s: str | None) -> dt.date | None:
        return dt.datetime.strptime(s, "%m/%d/%Y").date() if s else None

    location = e.get("Approved_Location") or [None, None]
    city, _state = (location + [None, None])[:2]

    return StagingCompetition(
        source=SOURCE,
        source_code=str(_require(e, "Comp_Year_ID", "compyears event")),
        name=_require(e, "Competition_Name", "compyears event"),
        start_date=parse_date(e.get("Start_Date")),
        end_date=parse_date(e.get("End_Date")),
        city=city,
        country="USA",  # NDCA Premier is a US-domestic circuit; state carried in `city` slot's sibling, not modeled separately
        sanctioning_body="NDCA",
        url=f"https://ndcapremier.com/results/?cyi={e['Comp_Year_ID']}",
        source_updated_at=_parse_publish_timestamp((e.get("Publish_Dates") or {}).get("Results")),
    )


def parse_competition(meta_json: bytes) -> StagingCompetition:
    """Parse a /feed/compyears/?cyi=<id> response."""
    payload = json.loads(meta_json)
    if payload.get("Status") != 1:
        raise ParseError(f"compyears feed returned Status={payload.get('Status')!r}")
    events = payload.get("Events") or []
    if not events:
        raise ParseError("compyears feed had no Events")
    return _competition_from_compyears_entry(events[0])


def parse_season_listing(season_json: bytes) -> list[StagingCompetition]:
    """Parse a /feed/compyears/?season=<N> response into one StagingCompetition
    per competition -- used by scripts/refresh_ndca.py to cheaply check every
    known competition's source_updated_at in a single request per season,
    rather than one detail request per competition."""
    payload = json.loads(season_json)
    if payload.get("Status") != 1:
        raise ParseError(f"season listing feed returned Status={payload.get('Status')!r}")
    # Publish_Results=0 means the organizer hasn't posted results yet -- same
    # filter bulk_load_ndca.discover_competitions applies; skip those rather
    # than treating "no results yet" as a competition needing a (fruitless) load.
    return [_competition_from_compyears_entry(e) for e in (payload.get("Events") or []) if e.get("Publish_Results")]


def _person_ref(participant: dict) -> StagingPersonRef:
    name_parts = _require(participant, "Name", "participant")
    return StagingPersonRef(name=" ".join(p for p in name_parts if p), external_ref=None)


def _parse_result_value(raw) -> float | int | None:
    """A competitor's Summary Result is a list, e.g. ["1"] or, when a tie needed
    breaking, ["TIE", "TIE", "3"] -- the last element is always the final
    resolved placement regardless of how many tie-break entries precede it.
    Returns None for a value that never resolved to a number (shouldn't
    normally happen for a completed round)."""
    if raw is None:
        return None
    try:
        f = float(raw)
    except (TypeError, ValueError):
        return None
    return int(f) if f.is_integer() else f


def _parse_event_dict(event: dict) -> NdcaEventData:
    event_id = _require(event, "ID", "event")
    raw_title = _require(event, "Name", "event")
    rounds_json = event.get("Rounds") or []
    if not rounds_json:
        raise ParseError(f"event {event_id!r} has no Rounds")

    # Rounds already arrive earliest-first (chronological); round_order = position.
    # entries_in/recalled_count/placements all come from each round's Summary,
    # NOT from Dances[0] -- Dances[N] carries only that one dance's per-dance
    # placement (which can differ from the round's true combined result, and
    # can itself be a tied fractional value like 2.5 for a single dance), while
    # Summary.Competitors[].Result is the round's real combined outcome. Using
    # Dances[0] was a confirmed real bug: e.g. a couple who *won a round
    # outright* (Summary Result "1") can still show a tied "2.5" in that
    # round's first dance alone if only that one dance had a tie.
    entries_in_by_order: list[int] = []
    recalled_count_by_order: list[int | None] = []
    for round_dict in rounds_json:
        summary_competitors = (round_dict.get("Summary") or {}).get("Competitors") or []
        entries_in_by_order.append(len(summary_competitors))
        recalled_values = [c.get("Recalled") for c in summary_competitors]
        if any(v is not None for v in recalled_values):
            recalled_count_by_order.append(sum(1 for v in recalled_values if v == 1))
        else:
            recalled_count_by_order.append(None)  # e.g. the Skated final: no recall concept

    staging_rounds: list[StagingRound] = []
    for i, round_dict in enumerate(rounds_json):
        staging_rounds.append(
            StagingRound(
                round_type=_require(round_dict, "Name", "round"),
                round_order=i + 1,
                entries_in=entries_in_by_order[i],
                recalled_count=recalled_count_by_order[i],
            )
        )

    last_round_summary = (rounds_json[-1].get("Summary") or {}).get("Competitors") or []
    placements_by_competitor: dict[str, float | int] = {}
    for c in last_round_summary:
        placement = _parse_result_value((c.get("Result") or [None])[-1])
        if placement is not None:
            placements_by_competitor[c["Bib"]] = placement
    finalist_bibs = {c["Bib"] for c in last_round_summary}

    field_size = entries_in_by_order[0]
    is_solo = False
    entries: list[StagingEntry] = []
    results: list[StagingResult] = []
    seen_bibs: set[str] = set()
    officials_by_letter: dict[str, StagingOfficial] = {}
    marks: list[StagingMark] = []

    for round_dict in rounds_json:
        round_label = round_dict["Name"]
        scoring_method = round_dict.get("Scoring_Method")
        for dance in round_dict.get("Dances") or []:
            dance_name = dance.get("Dance_Name")
            for judge in dance.get("Judges") or []:
                letter = _require(judge, "Judge_Letter", "judge")
                officials_by_letter.setdefault(
                    letter, StagingOfficial(letter=letter, name=" ".join(judge["Name"]), country=None, external_ref=None)
                )

            for c in dance.get("Competitors") or []:
                bib = _require(c, "Bib", "competitor-in-dance")
                participants = c.get("Participants") or []
                if not participants:
                    # A real (if rare) NDCA-side data gap, not a parsing bug --
                    # confirmed by cross-checking the live source: this exact
                    # bib/dance combo has no Participants in the source's own
                    # JSON. Skip just this one row rather than the whole
                    # event: since this same event's data is identical no
                    # matter which competitor's fetch it came through (every
                    # competitor in a heat sees the whole heat), raising here
                    # would make the entire event permanently unloadable from
                    # every possible fetch -- a real bug this replaced (see
                    # docs/ndca-format-notes.md).
                    logger.warning(
                        "skipping competitor with no Participants: bib=%r dance=%r round=%r",
                        bib,
                        dance_name,
                        round_label,
                    )
                    continue
                is_solo = is_solo or len(participants) == 1

                if bib not in seen_bibs:
                    seen_bibs.add(bib)
                    partner_1 = _person_ref(participants[0])
                    partner_2 = _person_ref(participants[1]) if len(participants) > 1 else None
                    entries.append(
                        StagingEntry(
                            competitor_no=bib,
                            country=None,
                            partner_1=partner_1,
                            partner_2=partner_2,
                            couple_ref=c.get("ID"),  # scoped to this event only -- see module docstring
                        )
                    )
                    placement = placements_by_competitor.get(bib)
                    results.append(
                        StagingResult(
                            competitor_no=bib,
                            placement_low=placement,
                            placement_high=placement,
                            made_final=bib in finalist_bibs,
                            field_size=field_size,
                        )
                    )

                marks_list = c.get("Marks") or []
                judges_list = dance.get("Judges") or []
                for judge, mark_value in zip(judges_list, marks_list):
                    marks.append(
                        StagingMark(
                            round_label=round_label,
                            competitor_no=bib,
                            judge_letter=judge["Judge_Letter"],
                            dance=dance_name,
                            recalled=(mark_value == 1) if scoring_method == "Prelims" else None,
                            placement=mark_value if scoring_method == "Skated" else None,
                        )
                    )

    # results with no placement recorded (didn't reach the final scored round)
    # still belong in `results` so field_size/made_final are on record; only
    # entries that got a real Result value carry a placement.
    ranking = RankingPage(is_solo=is_solo, rounds=staging_rounds, entries=entries, results=results)
    return NdcaEventData(
        source_code=str(event_id), raw_title=raw_title, ranking=ranking, officials=list(officials_by_letter.values()), marks=marks
    )


def parse_competitor_feed(results_json: bytes) -> list[NdcaEventData]:
    """Parse a /feed/results/?cyi=<id>&id=<competitor> response into one NdcaEventData per event.

    Each event is parsed independently: one malformed event (e.g. a
    competitor-in-dance row with no Participants -- a real, if rare, NDCA-side
    data gap, not a parsing bug) must not lose every *other* event in the same
    response. This was a real bug: since one competitor's fetch typically
    covers many events, and every one of those events' data is shared across
    every competitor who danced it (see module docstring / format notes), a
    single poisoned event previously failed the whole response -- and if
    every document containing that event carried the same bad row, the event
    silently never loaded at all, from any document, permanently. Skipped
    events are logged (not silent), just not fatal to their siblings.
    """
    payload = json.loads(results_json)
    if payload.get("Status") != 1:
        raise ParseError(f"results feed returned Status={payload.get('Status')!r}")
    result = payload.get("Result") or {}
    events = result.get("Events")
    if events is None:
        raise ParseError("competitor results feed missing Result.Events")

    parsed: list[NdcaEventData] = []
    for e in events:
        try:
            parsed.append(_parse_event_dict(e))
        except ParseError as exc:
            logger.warning("skipping unparseable event %r (%r): %s", e.get("ID"), e.get("Name"), exc)
    return parsed


def parse_event_feed(event_json: bytes) -> NdcaEventData:
    """Parse a /feed/results/?cyi=<id>&event=<id> response (one event)."""
    payload = json.loads(event_json)
    if payload.get("Status") != 1:
        raise ParseError(f"event results feed returned Status={payload.get('Status')!r}")
    event = (payload.get("Result") or {}).get("Event")
    if event is None:
        raise ParseError("event results feed missing Result.Event")
    return _parse_event_dict(event)


def parse_roster(roster_json: bytes) -> list[tuple[str, str]]:
    """Parse a /feed/results/?cyi=<id> roster response into (competitor_id, name) pairs.

    Also used for the heat-list flat attendee roster (/feed/heatlists/?cyi=<id>)
    -- same {ID, Name} shape, just with an extra "Type": "Attendee" field this
    function already ignores."""
    payload = json.loads(roster_json)
    if payload.get("Status") != 1:
        raise ParseError(f"roster feed returned Status={payload.get('Status')!r}")
    out = []
    for r in payload.get("Result") or []:
        name = " ".join(r.get("Name") or [])
        out.append((r["ID"], name))
    if not out:
        raise ParseError("roster feed produced zero competitors")
    return out


def _parse_round_time(s: str | None) -> dt.datetime | None:
    """NDCA's heat-list Round_Time values look like '9/6/2026 2:55:06 PM'."""
    return dt.datetime.strptime(s, "%m/%d/%Y %I:%M:%S %p") if s else None


def parse_heatlist_attendee(heatlist_json: bytes) -> list[StagingScheduledHeat]:
    """Parse a /feed/heatlists/?cyi=<id>&id=<attendee> response into one
    StagingScheduledHeat per (partnership, event, round) the attendee is
    scheduled for.

    Attendee-centric, unlike the results feed: the queried attendee is the
    top-level Name, and each "Entries" item is one of *their* partnerships
    (an attendee with several Pro-Am students has several Entries, each
    its own Couple_ID), naming only the *other* partner in Participants --
    confirmed on a real fixture (Matvii Artiushenko, 3 different partners
    at one competition). So unlike results, where every competitor's own
    fetch shows a stable, fixed Participants order for a given couple
    (see module docstring), which of the two ends up partner_1 here
    depends on which attendee happened to be queried -- callers that also
    load results must not assume this matches an existing Partnership's
    leader/follower order (see load/heatlist.py).

    An Entries item whose Type isn't "Partner", or whose Participants has
    more than one person (a formation/group entry, not a couple), is
    skipped -- logged, not fatal, same resilience pattern as
    parse_competitor_feed."""
    payload = json.loads(heatlist_json)
    if payload.get("Status") != 1:
        raise ParseError(f"heatlist feed returned Status={payload.get('Status')!r}")
    result = payload.get("Result") or {}
    attendee_name = _require(result, "Name", "heatlist attendee")
    partner_1 = StagingPersonRef(name=" ".join(p for p in attendee_name if p))

    out: list[StagingScheduledHeat] = []
    for entry in result.get("Entries") or []:
        if entry.get("Type") != "Partner":
            logger.warning("skipping heatlist entry with unsupported Type=%r", entry.get("Type"))
            continue
        participants = entry.get("Participants") or []
        if len(participants) > 1:
            logger.warning("skipping heatlist entry with %d participants (not a couple)", len(participants))
            continue
        partner_2 = _person_ref(participants[0]) if participants else None

        for event in entry.get("Events") or []:
            event_id = str(_require(event, "Event_ID", "heatlist event"))
            event_name = _require(event, "Event_Name", "heatlist event")
            for round_dict in event.get("Rounds") or []:
                round_name = round_dict.get("Round_Name")
                if not round_name:
                    logger.warning("skipping heatlist round with no Round_Name: event=%r", event_name)
                    continue
                out.append(
                    StagingScheduledHeat(
                        source_event_id=event_id,
                        event_name=event_name,
                        round_name=round_name,
                        heat_number=event.get("Heat"),
                        session=round_dict.get("Session"),
                        floor=event.get("Floor"),
                        competitor_no=event.get("Bib"),
                        scheduled_time=_parse_round_time(round_dict.get("Round_Time")),
                        is_complete=bool(round_dict.get("Complete")),
                        partner_1=partner_1,
                        partner_2=partner_2,
                    )
                )
    return out
