import json
from pathlib import Path

import pytest

from dsr.parse.cre8tive import (
    ParseError,
    build_event,
    group_events,
    parse_calendar,
    parse_competition,
    parse_competitors,
    parse_result_index,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "cre8tive"


def raw(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def detail(name: str) -> dict:
    return json.loads(raw(name))["data"]


@pytest.fixture(scope="module")
def names():
    return parse_competitors(raw("competitors.json"))


@pytest.fixture(scope="module")
def meta():
    return parse_competition(raw("meta.json"), country="United Kingdom")


def test_parse_competition(meta):
    staging, judges, hidden = meta
    assert staging.source == "cre8tive"
    assert staging.source_code == "2026105-international-dance-festival-2026-monday-dc47a543"
    assert staging.name == "International Dance Festival 2026 - Monday"
    assert str(staging.start_date) == "2026-10-05"
    assert staging.country == "United Kingdom"
    assert staging.url.endswith("/event/2026105-international-dance-festival-2026-monday-dc47a543/results")
    assert judges["1"] == "Morena Colagreco"
    assert hidden is False


def test_judge_titles_are_stripped():
    meta_json = json.dumps(
        {"data": {"id": "x", "name": "X", "date": "2026/10/3", "judgeNames": {"1": "Ms Morena Colagreco", "2": "Mr Gavin Chow"}}}
    ).encode()
    _, judges, _ = parse_competition(meta_json)
    assert judges == {"1": "Morena Colagreco", "2": "Gavin Chow"}


def test_parse_competitors_title_cases_upper_case_names(names):
    assert names[("M001", "1")] == "Nikita Vasilev and Diana Nagaitseva"


def test_competitor_name_markers_and_case():
    payload = json.dumps(
        {
            "data": [
                {"competitor": {"sku": "A", "bibNumber": "1", "name": "** STANISLAV ZELIANIN AND IRINA CHEREPANOVA"}},
                {"competitor": {"sku": "A", "bibNumber": "2", "name": "Nicolo Barbizi and Sara Ros Jakobsdottir"}},
                {"competitor": {"sku": "A", "bibNumber": "3", "name": "BILLIE O'NEIL-SMITH"}},
            ]
        }
    ).encode()
    parsed = parse_competitors(payload)
    assert parsed[("A", "1")] == "Stanislav Zelianin and Irina Cherepanova"
    assert parsed[("A", "2")] == "Nicolo Barbizi and Sara Ros Jakobsdottir"
    assert parsed[("A", "3")] == "Billie O'Neil-Smith"


def test_parse_result_index():
    ids = parse_result_index(raw("result_index.json"))
    assert "1790855226903-event-5-M006-auto-f" in ids


def test_parse_calendar_rejects_garbage():
    with pytest.raises(ParseError):
        parse_calendar(b'{"nope": 1}')
    with pytest.raises(ParseError):
        parse_calendar(b"<html>")


def _m006(names, meta, **kwargs):
    rows = [detail("detail_5_M006.json"), detail("detail_5_M006_auto_sf.json"), detail("detail_5_M006_auto_f.json")]
    (sku, title, grouped), = group_events(rows)
    assert sku == "M006"
    return build_event(sku, title, grouped, names, meta[1], **kwargs)


def test_rounds_ordered_and_labelled(names, meta):
    event, _ = _m006(names, meta)
    assert event.raw_title == "Int Ballroom - O50"
    assert [(r.round_type, r.round_order) for r in event.ranking.rounds] == [
        ("Quarter-final", 1),
        ("Semi-final", 2),
        ("Final", 3),
    ]
    quarter, semi, final = event.ranking.rounds
    assert quarter.entries_in == 17 and quarter.recalled_count == 14
    assert final.recalled_count is None


def test_final_placements_and_results(names, meta):
    event, _ = _m006(names, meta)
    by_no = {r.competitor_no: r for r in event.ranking.results}
    winners = sorted((r.placement_low, r.competitor_no) for r in by_no.values() if r.made_final)
    assert [p for p, _ in winners] == list(range(1, len(winners) + 1))
    assert all(r.placement_low is None for r in by_no.values() if not r.made_final)
    assert all(r.field_size == 17 for r in by_no.values())
    assert len(by_no) == len(event.ranking.entries) == 17


def test_stray_summary_row_without_a_competitor_is_skipped(names, meta):
    # M006's quarter-final summary lists couple 38, who is in neither the
    # event's competitor list nor the competitors feed.
    event, _ = _m006(names, meta)
    assert event.skipped_couples >= 1
    assert "38" not in {e.competitor_no for e in event.ranking.entries}
    assert all(m.competitor_no != "38" for m in event.marks)


def test_entries_split_into_partners(names, meta):
    event, _ = _m006(names, meta)
    entry = next(e for e in event.ranking.entries if e.partner_2 is not None)
    assert entry.partner_1.name and entry.partner_2.name
    assert "and" not in entry.partner_1.name.lower().split()


def test_final_marks_are_per_judge_placements(names, meta):
    event, judges = _m006(names, meta)
    final = [m for m in event.marks if m.round_label == "Final"]
    # 17 judges x 4 dances x the couples in the final
    n_final = sum(1 for r in event.ranking.results if r.made_final)
    assert len(final) == 17 * 4 * n_final
    assert {m.dance for m in final} == {"Int'l Waltz", "Int'l Tango", "Int'l Foxtrot", "Int'l Quickstep"}
    assert all(m.recalled is None and 1 <= m.placement <= n_final for m in final)
    # one placement per (couple, dance, judge) -- the key the mark table is unique on
    assert len({(m.competitor_no, m.dance, m.judge_letter) for m in final}) == len(final)
    assert judges["1"] == "Morena Colagreco"


def test_recall_marks_cover_every_couple_and_match_totals(names, meta):
    event, _ = _m006(names, meta)
    semi = [m for m in event.marks if m.round_label == "Semi-final"]
    semi_detail = detail("detail_5_M006_auto_sf.json")
    summary = {str(r["coupleId"]): r for r in semi_detail["eventScores"]["roundResultsSummary"]}
    judges = {m.judge_letter for m in semi}
    assert len(semi) == len(judges) * 4 * len(summary)
    assert all(m.recalled in (True, False) and m.placement is None for m in semi)
    # a couple's Y count over all dances == the source's own totalMarks for it
    for couple, row in summary.items():
        assert sum(1 for m in semi if m.competitor_no == couple and m.recalled) == row["totalMarks"]


def test_only_judges_listed_for_the_event_and_with_data_get_marks(names, meta):
    sf = detail("detail_5_M006_auto_sf.json")
    listed = set(sf["event"]["judges"].split(","))
    event, _ = _m006(names, meta)
    used = {m.judge_letter for m in event.marks if m.round_label == "Semi-final"}
    assert used and used <= listed


def test_hesitations_are_parsed_for_recall_rounds_only(names, meta):
    event, _ = _m006(names, meta)
    assert event.hesitations
    assert {h.round_label for h in event.hesitations} <= {"Quarter-final", "Semi-final"}
    sf = detail("detail_5_M006_auto_sf.json")
    expected = sum(
        1
        for key, scored in sf["eventScores"].items()
        if key.startswith("judge_") and key.removeprefix("judge_") in sf["event"]["judges"].split(",")
        and (scored.get("submittedAt") or any((scored.get("picks") or {}).values()))
        for flagged in scored["hesitations"].values()
        for couple in flagged
        if ("M006", str(couple)) in names
    )
    assert sum(1 for h in event.hesitations if h.round_label == "Semi-final") == expected
    # every flag sits on a couple/judge/dance that also has an ordinary recall mark
    marked = {(m.round_label, m.competitor_no, m.judge_letter, m.dance) for m in event.marks}
    assert all((h.round_label, h.competitor_no, h.judge_letter, h.dance) in marked for h in event.hesitations)


def test_hidden_judge_scores_yield_no_marks(names, meta):
    event, judges = _m006(names, meta, hide_judge_scores=True)
    assert event.marks == []
    assert event.hesitations == []
    assert judges == {}
    assert any(r.made_final for r in event.ranking.results)


def test_couples_without_a_name_are_skipped_not_fatal(meta):
    rows = [detail("detail_0_M001_auto_f.json")]
    names_missing_one = {k: v for k, v in parse_competitors(raw("competitors.json")).items() if k != ("M001", "1")}
    (sku, title, grouped), = group_events(rows)
    event, _ = build_event(sku, title, grouped, names_missing_one, meta[1])
    assert event.skipped_couples == 1
    assert "1" not in {e.competitor_no for e in event.ranking.entries}
    assert all(m.competitor_no != "1" for m in event.marks)


def test_title_collision_appends_sku():
    def row(sku):
        return {"event": {"sku": sku, "categoryEn": "Int Latin - Professional", "rounds": "F"}}

    titles = {t for _, t, _ in group_events([row("A1"), row("B2")])}
    assert titles == {"Int Latin - Professional [A1]", "Int Latin - Professional [B2]"}
    assert {t for _, t, _ in group_events([row("A1")])} == {"Int Latin - Professional"}


def test_solo_entries():
    names = {("S", "1"): "Billie McHugh", ("S", "2"): "Freya Saker"}
    data = {
        "event": {"sku": "S", "rounds": "F", "dances": "W", "categoryEn": "U8 SOLO-W", "judges": ""},
        "result": {
            "overallFinalResults": [
                {"coupleId": 1, "overallFinalRank": 1},
                {"coupleId": 2, "overallFinalRank": 2},
            ],
            "perDanceResults": [{"danceName": "W", "judges": [], "results": []}],
        },
    }
    event, _ = build_event("S", "U8 SOLO-W", [data], names, {})
    assert event.ranking.is_solo
    assert all(e.partner_2 is None for e in event.ranking.entries)
    assert [r.placement_low for r in event.ranking.results] == [1, 2]


def test_load_end_to_end_and_idempotent(tmp_path, names, meta):
    from sqlalchemy import func, select

    from dsr.db import get_session, init_db
    from dsr.load.cre8tive import load_event
    from dsr.load.wdsf import load_competition, load_officials
    from dsr.models import CompEvent, Entry, Mark, MarkHesitation, Result, Round
    from dsr.parse.cre8tive import officials_from

    session = get_session(init_db(tmp_path / "cre8tive.sqlite3"))
    staging, meta_judges, _ = meta

    def run():
        event, judges = _m006(names, meta)
        competition = load_competition(session, staging)
        judge_ids = load_officials(session, "cre8tive", officials_from({**meta_judges, **judges}))
        load_event(session, competition, event, judge_ids)
        session.commit()
        return event

    event = run()
    counts = lambda: tuple(  # noqa: E731
        session.scalar(select(func.count()).select_from(m)) for m in (CompEvent, Round, Entry, Result, Mark)
    )
    first = counts()
    n_hesitations = session.scalar(select(func.count()).select_from(MarkHesitation))
    assert n_hesitations == len({(h.round_label, h.competitor_no, h.judge_letter, h.dance) for h in event.hesitations}) > 0
    assert first == (1, 3, 17, 17, len(event.marks))

    # winner is a real couple with a Final round and per-judge placements
    winner = session.scalar(select(Result).where(Result.placement_low == 1))
    assert winner.made_final
    placements = session.scalars(
        select(Mark.placement).where(Mark.entry_id == winner.entry_id, Mark.placement.is_not(None))
    ).all()
    assert len(placements) == 17 * 4

    run()
    assert counts() == first  # re-running changes nothing
    assert session.scalar(select(func.count()).select_from(MarkHesitation)) == n_hesitations


def test_fourth_round_is_kept_in_order():
    names = {("E", "1"): "A One and B One", ("E", "2"): "C Two and D Two"}

    def rnd(code):
        return {
            "event": {"sku": "E", "rounds": code, "dances": "W", "categoryEn": "Int Latin - Open", "judges": ""},
            "eventScores": {"roundResultsSummary": [{"coupleId": 1, "advanced": True}, {"coupleId": 2, "advanced": False}]},
        }

    event, _ = build_event("E", "Int Latin - Open", [rnd("QF"), rnd("4R"), rnd("1R")], names, {})
    assert [r.round_type for r in event.ranking.rounds] == ["Round 1", "Round 4", "Quarter-final"]
