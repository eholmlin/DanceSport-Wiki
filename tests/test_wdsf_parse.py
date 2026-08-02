import datetime as dt
from pathlib import Path

import pytest

from dsr.parse.wdsf import (
    ParseError,
    parse_event_page,
    parse_final_page,
    parse_marks_page,
    parse_officials_page,
    parse_ranking_page,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "wdsf"


def load(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


# ---------- event page ----------


def test_event_page_large_multidiscipline_stop():
    html = load("event_taipei_2026.html")
    url = "https://worlddancesport.org/Events/Taipei-Chinese-Taipei-01082026-02082026-8668"
    competition, comp_events = parse_event_page(html, url)

    assert competition.source == "wdsf"
    assert competition.source_code == "Taipei-Chinese-Taipei-01082026-02082026-8668"
    assert competition.city == "Taipei"
    assert competition.country == "Chinese Taipei"
    assert competition.start_date == dt.date(2026, 7, 31)
    assert competition.end_date == dt.date(2026, 8, 2)
    assert competition.sanctioning_body == "WDSF"

    assert len(comp_events) == 22
    latin = next(c for c in comp_events if c.source_code == "Open-Taipei-Adult-Latin-66537")
    assert latin.raw_title == "WDSF Open Latin Adult"
    assert latin.marks_url == "/Competitions/Marks/Open-Taipei-Adult-Latin-66537"
    assert latin.final_url == "/Competitions/Final/Open-Taipei-Adult-Latin-66537"


def test_event_page_small_historical_stop():
    html = load("event_charlotte_2015.html")
    url = "https://worlddancesport.org/Events/Charlotte-United-States-02102015-03102015-6475"
    competition, comp_events = parse_event_page(html, url)

    assert competition.start_date == dt.date(2015, 10, 2)
    assert competition.end_date == dt.date(2015, 10, 3)
    codes = {c.source_code for c in comp_events}
    assert "Open-Charlotte-Senior-I-Latin-47782" in codes


def test_event_page_rejects_garbage():
    with pytest.raises(ParseError):
        parse_event_page(b"<html><body>not an event page</body></html>", "https://x/y")


# ---------- ranking page ----------


def test_ranking_large_field_multi_round():
    page = parse_ranking_page(load("ranking_taipei_adult_latin_2026.html"))

    assert page.is_solo is False
    assert [r.round_type for r in page.rounds] == ["1. Round", "2. Round", "final"]
    round1, round2, final = page.rounds
    assert (round1.entries_in, round1.recalled_count) == (19, 12)
    assert (round2.entries_in, round2.recalled_count) == (12, 6)
    assert (final.entries_in, final.recalled_count) == (6, None)

    # field_size on every result must equal round-1 entries (spec: weighting needs this)
    assert all(r.field_size == 19 for r in page.results)

    winner = next(r for r in page.results if r.competitor_no == "19")
    assert (winner.placement_low, winner.placement_high, winner.made_final) == (1, 1, True)

    # tie band, e.g. "8. - 9."
    tied = [r for r in page.results if r.placement_low == 8]
    assert tied and tied[0].placement_high == 9

    # excused couple present as an entry with no result
    excused_names = {e.partner_1.name for e in page.entries if e.competitor_no == ""}
    assert "Wilbert Aunzo" in excused_names
    assert len(page.results) == 19  # excused couple contributes no result row

    # athlete GUIDs and couple ids captured for entity resolution
    winner_entry = next(e for e in page.entries if e.competitor_no == "19")
    assert winner_entry.partner_1.external_ref == "e76e95f4-a2c1-4ba2-88c8-a7e6002034e0"
    assert winner_entry.couple_ref == "593333"


def test_ranking_small_final_only_field():
    page = parse_ranking_page(load("ranking_charlotte_seniorI_latin_2015.html"))

    assert [r.round_type for r in page.rounds] == ["final"]
    assert page.rounds[0].entries_in == 3
    assert len(page.results) == 3
    assert {r.placement_low for r in page.results} == {1, 2, 3}
    assert all(r.field_size == 3 for r in page.results)


def test_ranking_solo_event_has_no_partner_2():
    page = parse_ranking_page(load("ranking_taipei_adult_solo_latin_female_2026.html"))

    assert page.is_solo is True
    assert len(page.entries) == 32
    assert all(e.partner_2 is None for e in page.entries)
    assert all(e.couple_ref is None for e in page.entries)


def test_ranking_page_rejects_garbage():
    with pytest.raises(ParseError):
        parse_ranking_page(b"<html><body>no rounds here</body></html>")


# ---------- officials page ----------


def test_officials_page_maps_letters_to_names():
    officials = parse_officials_page(load("officials_taipei_adult_latin_2026.html"))

    assert len(officials) == 11
    by_letter = {o.letter: o for o in officials}
    assert by_letter["A"].name == "Natalia Urban"
    assert by_letter["A"].country == "OIN"
    assert by_letter["A"].external_ref == "db22c5f7-923e-42df-bcd7-9e140120b1a8"
    assert {o.letter for o in officials} == set("ABCDEFGHIJK")


def test_officials_page_rejects_garbage():
    with pytest.raises(ParseError):
        parse_officials_page(b"<html><body>no adjudicators table</body></html>")


# ---------- marks page (recall rounds) ----------


def test_marks_page_large_field():
    marks = parse_marks_page(load("marks_taipei_adult_latin_2026.html"))

    # (round1 entries=19 + round2 entries=12) couple-rounds * 5 dances * 11 judges
    assert len(marks) == (19 + 12) * 5 * 11

    winner_round1 = [m for m in marks if m.competitor_no == "19" and m.round_order == 1]
    assert len(winner_round1) == 5 * 11
    assert {m.dance for m in winner_round1} == {"Samba", "Cha Cha Cha", "Rumba", "Paso Doble", "Jive"}
    assert all(m.recalled is not None for m in winner_round1)  # every cell resolves to True/False, never unknown
    assert all(m.placement is None for m in winner_round1)  # recall rounds carry no ordinal placement

    # cross-check against the page's own per-dance recall subtotal (dSum): 9 in Samba
    # (judges E and K didn't recall), 10 in the other four dances (only K didn't) --
    # this is what actually caught a cell-misalignment bug during development.
    by_dance = {}
    for m in winner_round1:
        by_dance.setdefault(m.dance, []).append(m.recalled)
    assert sum(by_dance["Samba"]) == 9
    for dance in ("Cha Cha Cha", "Rumba", "Paso Doble", "Jive"):
        assert sum(by_dance[dance]) == 10
    judge_k = {m.dance: m.recalled for m in winner_round1 if m.judge_letter == "K"}
    assert all(v is False for v in judge_k.values())  # judge K didn't recall this couple in any dance


def test_marks_page_rejects_garbage():
    with pytest.raises(ParseError):
        parse_marks_page(b"<html><body>no marks table</body></html>")


def test_marks_page_empty_when_no_recall_rounds():
    # 3-couple field that went straight to Final: the Marks (recall-round) page
    # renders dance columns but zero per-judge sub-columns -- valid, not an error.
    marks = parse_marks_page(load("marks_charlotte_seniorI_latin_2015.html"))
    assert marks == []


# ---------- final page (aggregate skating-system placement) ----------


def test_final_page_one_row_per_judge_per_couple():
    marks = parse_final_page(load("final_taipei_adult_latin_2026.html"))

    assert len(marks) == 6 * 11  # 6 finalists * 11 judges
    winner_marks = [m for m in marks if m.competitor_no == "19"]
    assert len(winner_marks) == 11
    assert all(m.dance is None for m in winner_marks)  # combined placement, not per-dance
    assert all(m.round_order is None for m in winner_marks)
    assert {m.placement for m in winner_marks} <= set(range(1, 7))


def test_final_page_rejects_garbage():
    with pytest.raises(ParseError):
        parse_final_page(b"<html><body>no skating table</body></html>")


def test_final_page_small_field_nonconsecutive_judge_letters():
    # Only 7 of 11 possible letters were on this panel (A,B,C,E,F,I,J -- D,G,H,K
    # absent), confirming the parser must not assume a contiguous A..K panel.
    marks = parse_final_page(load("final_charlotte_seniorI_latin_2015.html"))
    assert len(marks) == 3 * 7
    assert {m.judge_letter for m in marks} == {"A", "B", "C", "E", "F", "I", "J"}
