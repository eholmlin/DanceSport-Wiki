import datetime as dt
from pathlib import Path

import pytest

from dsr.parse.ndca import (
    ParseError,
    parse_competition,
    parse_competitor_feed,
    parse_event_feed,
    parse_roster,
    parse_season_listing,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "ndca"


def load(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def test_parse_competition_metadata():
    comp = parse_competition(load("compyears_by_cyi_1612.json"))
    assert comp.source == "ndca_premier"
    assert comp.source_code == "1612"
    assert comp.name == "Austin Star Ball"
    assert comp.start_date == dt.date(2025, 1, 11)
    assert comp.sanctioning_body == "NDCA"


def test_parse_competition_rejects_garbage():
    with pytest.raises(ParseError):
        parse_competition(b'{"Status": 1, "Events": []}')


def test_parse_roster():
    roster = parse_roster(load("roster_cyi1612.json"))
    assert len(roster) == 63
    assert ("A251", "Mary Ann Alberry") in roster


def test_parse_roster_rejects_garbage():
    with pytest.raises(ParseError):
        parse_roster(b'{"Status": 1, "Result": []}')


def test_parse_competitor_feed_walkover():
    events = parse_competitor_feed(load("competitor_A203_walkover.json"))
    assert len(events) == 1
    e = events[0]
    assert len(e.ranking.rounds) == 1
    assert e.ranking.rounds[0].round_type == "Final"
    assert e.ranking.rounds[0].entries_in == 1
    assert len(e.ranking.entries) == 1
    assert e.ranking.entries[0].partner_1.name == "Irsan Tisnabudi"
    assert e.ranking.entries[0].partner_1.external_ref is None  # NDCA ids never trusted as external refs


def test_parse_competitor_feed_multi_event_small_final_only():
    events = parse_competitor_feed(load("competitor_A251_multi_event.json"))
    assert len(events) == 22

    single_dance = next(e for e in events if e.source_code == "99")
    assert single_dance.raw_title == "Single Dance Events L-Sr1 Closed Full Bronze Amer. Waltz"
    assert [r.round_type for r in single_dance.ranking.rounds] == ["Final"]
    assert single_dance.ranking.rounds[0].entries_in == 3
    placements = {r.competitor_no: r.placement_low for r in single_dance.ranking.results}
    assert placements == {"112": 1, "113": 3, "128": 2}
    assert all(r.made_final for r in single_dance.ranking.results)

    letters = {o.letter for o in single_dance.officials}
    assert letters == {"02", "03", "04"}
    assert all(o.external_ref is None for o in single_dance.officials)

    # Skated final round: marks carry ordinal placement, not recall booleans
    assert all(m.placement is not None and m.recalled is None for m in single_dance.marks)
    assert len(single_dance.marks) == 3 * 3  # 3 couples * 3 judges


def test_parse_competitor_feed_multiround_semifinal_recall():
    events = parse_competitor_feed(load("competitor_A174_multiround_pro.json"))
    e = next(ev for ev in events if ev.source_code == "847")

    assert [r.round_type for r in e.ranking.rounds] == ["Semi-Final", "Final"]
    semi, final = e.ranking.rounds
    assert (semi.entries_in, semi.recalled_count) == (9, 6)
    assert (final.entries_in, final.recalled_count) == (6, None)
    assert e.ranking.results[0].field_size == 9

    # Prelims round marks are recall booleans, not placements
    prelim_marks = [m for m in e.marks if m.round_label == "Semi-Final"]
    assert prelim_marks and all(m.recalled is not None and m.placement is None for m in prelim_marks)

    # non-finalists get no placement but are still on record with made_final=False
    non_finalists = [r for r in e.ranking.results if not r.made_final]
    assert len(non_finalists) == 3
    assert all(r.placement_low is None for r in non_finalists)

    winner = next(r for r in e.ranking.results if r.placement_low == 1)
    assert winner.made_final is True


def test_per_event_feed_matches_per_competitor_feed_for_same_event():
    via_competitor = [e for e in parse_competitor_feed(load("competitor_A174_multiround_pro.json")) if e.source_code == "847"][0]
    via_event = parse_event_feed(load("event_847_by_event_param.json"))

    assert via_competitor.raw_title == via_event.raw_title
    assert via_competitor.ranking.rounds == via_event.ranking.rounds
    assert sorted(via_competitor.ranking.results, key=lambda r: r.competitor_no) == sorted(
        via_event.ranking.results, key=lambda r: r.competitor_no
    )
    assert len(via_competitor.marks) == len(via_event.marks)


def test_event_feed_rejects_garbage():
    with pytest.raises(ParseError):
        parse_event_feed(b'{"Status": 1, "Result": {}}')


# ---------- regression: overall placement must come from Round.Summary, ----------
# ---------- never from Dances[0] (a single dance's own tied/partial result) -------


def test_placement_uses_round_summary_not_first_dance_result():
    # Real case: bib 140 tied 2.5 in the *first dance only* (Amer. Cha Cha) of
    # a 5-dance final, but Round.Summary (the true combined result across all
    # 5 dances) says they won outright, "Result": ["1"]. The old bug read
    # Dances[0]'s per-dance value directly and stored 2.5 as if it were the
    # overall placement.
    events = parse_competitor_feed(load("competitor_A99_summary_vs_first_dance_tie.json"))
    e = next(ev for ev in events if ev.raw_title == "Open Scholarship B M/F Amer. Rhythm #NR (CC,R,SW,B,M)")
    winner = next(r for r in e.ranking.results if r.competitor_no == "140")
    assert (winner.placement_low, winner.placement_high) == (1, 1)
    assert winner.made_final is True


def test_placement_uses_round_summary_across_multidance_final():
    # Real case: a 3-dance Final (Cha Cha, Samba, Rumba) where the couple's
    # per-dance results were 2/3/3 -- Dances[0] alone (Cha Cha=2) is not their
    # overall placement. Round.Summary gives the correct combined result: 3.
    events = parse_competitor_feed(load("competitor_A4591_multidance_final_summary.json"))
    e = next(ev for ev in events if ev.raw_title == "Youth Pre-Championship Latin  (CC,S,R)")
    result = next(r for r in e.ranking.results if r.competitor_no == "415")
    assert (result.placement_low, result.placement_high) == (3, 3)
    assert result.made_final is True


def test_result_value_parsing():
    from dsr.parse.ndca import _parse_result_value

    assert _parse_result_value("1") == 1
    assert _parse_result_value("2.5") == 2.5
    assert _parse_result_value("3") == 3
    assert _parse_result_value(None) is None
    assert _parse_result_value("TIE") is None


# ---------- incremental refresh: source_updated_at + season listing ----------


def test_parse_competition_captures_source_updated_at():
    comp = parse_competition(load("compyears_by_cyi_1612.json"))
    assert comp.source_updated_at == dt.datetime(2025, 1, 11, 19, 51, 6)


def test_parse_season_listing_returns_one_competition_per_entry():
    comps = parse_season_listing(load("compyears_season42.json"))
    assert len(comps) > 0
    assert all(c.source for c in comps)
    american_star_ball = next(c for c in comps if c.source_code == "476")
    assert american_star_ball.name == "American Star Ball Championships"
    assert american_star_ball.source_updated_at == dt.datetime(2026, 5, 17, 10, 11, 44)


def test_parse_season_listing_skips_unpublished_results():
    import json

    payload = json.loads(load("compyears_season42.json"))
    unpublished_ids = {str(e["Comp_Year_ID"]) for e in payload["Events"] if not e.get("Publish_Results")}
    assert unpublished_ids  # sanity: the fixture actually has some unpublished entries

    comps = parse_season_listing(load("compyears_season42.json"))
    returned_ids = {c.source_code for c in comps}
    assert returned_ids.isdisjoint(unpublished_ids)


def test_parse_season_listing_rejects_garbage():
    with pytest.raises(ParseError):
        parse_season_listing(b'{"Status": 0}')
