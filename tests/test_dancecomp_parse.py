from pathlib import Path

import pytest

from dsr.parse.dancecomp import ParseError, parse_result
from dsr.parse.staging import StagingResult

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "dancecomp"


def load(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def test_parse_result_rejects_garbage():
    with pytest.raises(ParseError):
        parse_result(b"<html><body>nothing here</body></html>")


def test_parse_result_extracts_embedded_json_and_groups_by_heat_and_round():
    events, skipped = parse_result(load("HC2023_124_result.htm"))
    assert skipped == 0
    assert len(events) > 40
    titles = {e.raw_title for e in events}
    assert "Heat 1: L-B1 Newcomer Am. West Coast Swing Final" not in titles  # heat number + round suffix stripped
    assert "L-B1 Newcomer Am. West Coast Swing" in titles  # "Final" suffix stripped too
    assert "L-C1 Proficiency Pre Bronze Am. Merengue" in titles  # no recall structure -- no suffix to strip


def test_parse_result_final_round_has_numeric_placements():
    events, _skipped = parse_result(load("HC2023_124_result.htm"))
    event = next(e for e in events if e.raw_title == "L-B1 Newcomer Am. West Coast Swing")
    round_ = event.ranking.rounds[0]
    assert round_.round_type == "Final"
    assert round_.entries_in == 1

    assert event.ranking.results == [
        StagingResult(competitor_no="214", placement_low=1, placement_high=1, made_final=True, field_size=1)
    ]
    entry = event.ranking.entries[0]
    assert entry.partner_1.name == "Brandon DeLong"
    assert entry.partner_2.name == "Brigette Lopez"
    assert event.marks == []  # no per-judge data in this source


def test_parse_result_quarter_final_outcome_has_no_numeric_placement():
    # Real case: "Heat 391: L-M Pro/Am Closed Bronze American Rhythm
    # Championships (C/R/SW) - Quarter-final" with place="QF" -- the
    # couple reached the quarter-final round but wasn't recalled further;
    # there's no numeric rank for this in the source at all, only the
    # round they were eliminated at, so made_final=False with
    # placement_low=None (same convention as a "not recalled" case
    # elsewhere in this project).
    events, _skipped = parse_result(load("HC2023_124_result.htm"))
    event = next(
        e
        for e in events
        if e.raw_title == "L-M Pro/Am Closed Bronze American Rhythm Championships (C/R/SW)"
        and e.ranking.rounds[0].round_type == "Quarter-final"
    )
    assert event.ranking.results == [
        StagingResult(competitor_no="294", placement_low=None, placement_high=None, made_final=False, field_size=1)
    ]


def test_parse_result_multi_couple_field_gets_distinct_placements():
    events, _skipped = parse_result(load("HC2023_124_result.htm"))
    event = next(e for e in events if e.raw_title == "L-C1 Pre Bronze Am. West Coast Swing")
    round_ = event.ranking.rounds[0]
    assert round_.entries_in == 3
    placements = sorted(r.placement_low for r in event.ranking.results)
    assert placements == [1, 2, 3]
