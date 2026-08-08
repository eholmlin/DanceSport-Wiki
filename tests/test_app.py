"""Regression tests for app.py query helpers -- separate from the parse/load
pipeline tests, since these bugs live in how the Streamlit app queries
already-loaded data.
"""
import numpy as np
import pandas as pd

from app import (
    _highest_round_labels_for_entries,
    _ordinal,
    _skating_system_rank,
    best_results,
    marks_detail_for_entry,
    marks_detail_with_totals,
    partner_category,
    results_for_comp_event,
    skating_system_results_for_final,
)

from dsr.db import get_session, init_db
from dsr.models import CompEvent, Competition, Entry, Mark, Partnership, Person, Result, Round


def test_highest_round_is_scoped_per_comp_event_not_shared_entry(tmp_path):
    # Real case: NDCA's Entry row is (competition, partnership)-scoped, not
    # per-event (see load/wdsf.py docstring), so a couple entered in several
    # events at the same competition shares one entry_id across all of them.
    # A couple made the Final in one event but was eliminated in Round 1 of
    # another event at the same competition -- the old code, keyed on
    # entry_id alone, leaked the Final from the first event into the
    # "highest round" shown for the second, producing the contradiction a
    # user reported: "highest round: Final" next to placement "not recalled".
    engine = init_db(tmp_path / "highest_round.sqlite3")
    session = get_session(engine)

    competition = Competition(source="ndca_premier", source_code="1", name="Test Comp")
    session.add(competition)
    session.flush()

    leader = Person(display_name="Leader")
    follower = Person(display_name="Follower")
    session.add_all([leader, follower])
    session.flush()

    partnership = Partnership(leader_id=leader.id, follower_id=follower.id, kind="amateur")
    session.add(partnership)
    session.flush()

    # One shared Entry row for both events, as NDCA loading actually produces.
    entry = Entry(competition_id=competition.id, partnership_id=partnership.id, competitor_no="1")
    session.add(entry)
    session.flush()

    event_made_final = CompEvent(competition_id=competition.id, raw_title="Event A (made Final)")
    event_round1_only = CompEvent(competition_id=competition.id, raw_title="Event B (Round 1 only)")
    session.add_all([event_made_final, event_round1_only])
    session.flush()

    round1_a = Round(comp_event_id=event_made_final.id, round_type="Round 1", round_order=1)
    final_a = Round(comp_event_id=event_made_final.id, round_type="final", round_order=2)
    round1_b = Round(comp_event_id=event_round1_only.id, round_type="Round 1", round_order=1)
    session.add_all([round1_a, final_a, round1_b])
    session.flush()

    session.add_all(
        [
            Mark(round_id=round1_a.id, entry_id=entry.id, dance="Waltz", recalled=True),
            Mark(round_id=final_a.id, entry_id=entry.id, dance="Waltz", placement=1),
            Mark(round_id=round1_b.id, entry_id=entry.id, dance="Waltz", recalled=False),
        ]
    )
    session.commit()

    result = _highest_round_labels_for_entries(
        session, [(entry.id, event_made_final.id), (entry.id, event_round1_only.id)]
    )

    assert result[(entry.id, event_made_final.id)] == (final_a.id, 2, "Final")
    assert result[(entry.id, event_round1_only.id)] == (round1_b.id, 1, "Round 1")


def test_marks_detail_accepts_numpy_int_entry_id(tmp_path):
    # Real case: results_for_comp_event's dataframe stores _entry_id as a
    # pandas column, so a couple selected via a Streamlit selectbox built
    # from that dataframe hands marks_detail_for_entry a numpy.int64, not a
    # plain int. Mark.entry_id == entry_id silently matched zero rows with
    # a numpy.int64 (SQLAlchemy didn't error, just returned nothing) --
    # found by comparing a hardcoded plain int (300 rows) against the same
    # id pulled from a real results_for_comp_event() dataframe (0 rows).
    engine = init_db(tmp_path / "numpy_entry_id.sqlite3")
    session = get_session(engine)

    competition = Competition(source="ndca_premier", source_code="1", name="Test Comp")
    session.add(competition)
    session.flush()

    leader = Person(display_name="Leader")
    follower = Person(display_name="Follower")
    session.add_all([leader, follower])
    session.flush()

    partnership = Partnership(leader_id=leader.id, follower_id=follower.id, kind="amateur")
    session.add(partnership)
    session.flush()

    entry = Entry(competition_id=competition.id, partnership_id=partnership.id, competitor_no="1")
    session.add(entry)
    session.flush()

    event = CompEvent(competition_id=competition.id, raw_title="Event A")
    session.add(event)
    session.flush()

    final_round = Round(comp_event_id=event.id, round_type="final", round_order=1)
    session.add(final_round)
    session.flush()

    session.add(Mark(round_id=final_round.id, entry_id=entry.id, dance="Waltz", placement=1))
    session.commit()

    numpy_entry_id = np.int64(entry.id)
    result = marks_detail_for_entry(session, numpy_entry_id, event.id)

    assert len(result) == 1


def test_skating_system_rank_requires_majority_not_just_sum():
    # 3 competitors, 4 judges giving a full 1-2-3 ranking each. B wins 1st
    # with an outright majority of "1st-or-2nd" votes (4/4) even though
    # that's a different reason than "lowest sum" -- and placing 2nd
    # requires expanding the threshold from 1 to 2 among the remaining
    # competitors {A, C}, exercising the core majority-rule loop rather than
    # a simple sort by sum.
    votes = {
        "A": [1, 2, 1, 3],
        "B": [2, 1, 2, 1],
        "C": [3, 3, 3, 2],
    }
    result = _skating_system_rank(votes)
    assert result == {"B": 1, "A": 2, "C": 3}


def test_skating_system_rank_breaks_ties_by_sum():
    # Both competitors reach the same vote count (2 of 3) at threshold=1,
    # a genuine tie at the majority-rule step -- broken by comparing their
    # own summed placements (lower wins), the documented fallback.
    votes = {1: [1, 1, 3], 2: [1, 1, 2]}
    result = _skating_system_rank(votes)
    assert result == {2: 1, 1: 2}


def test_skating_system_results_for_final_finds_ndca_style_round_type(tmp_path):
    # Real bug: NDCA's Round.round_type is stored as "Final" (as-is from
    # source), not the lowercase "final" WDSF uses. A case-sensitive
    # Round.round_type == "final" filter silently found no Final round at
    # all for any NDCA competition, making skating_system_results_for_final
    # return empty for every NDCA event -- caught by running it against a
    # real NDCA comp_event and getting {} back instead of real placements.
    engine = init_db(tmp_path / "ndca_round_case.sqlite3")
    session = get_session(engine)

    competition = Competition(source="ndca_premier", source_code="1", name="Test Comp")
    session.add(competition)
    session.flush()

    people = [Person(display_name=f"Person {i}") for i in range(4)]
    session.add_all(people)
    session.flush()

    partnership_a = Partnership(leader_id=people[0].id, follower_id=people[1].id, kind="amateur")
    partnership_b = Partnership(leader_id=people[2].id, follower_id=people[3].id, kind="amateur")
    session.add_all([partnership_a, partnership_b])
    session.flush()

    entry_a = Entry(competition_id=competition.id, partnership_id=partnership_a.id, competitor_no="1")
    entry_b = Entry(competition_id=competition.id, partnership_id=partnership_b.id, competitor_no="2")
    session.add_all([entry_a, entry_b])
    session.flush()

    event = CompEvent(competition_id=competition.id, raw_title="Event A")
    session.add(event)
    session.flush()

    # NDCA's own round label casing, not WDSF's lowercase "final".
    final_round = Round(comp_event_id=event.id, round_type="Final", round_order=1)
    session.add(final_round)
    session.flush()

    session.add_all(
        [
            Mark(round_id=final_round.id, entry_id=entry_a.id, dance="Waltz", placement=1),
            Mark(round_id=final_round.id, entry_id=entry_b.id, dance="Waltz", placement=2),
        ]
    )
    session.commit()

    per_dance = skating_system_results_for_final(session, event.id)
    assert per_dance == {"Waltz": {entry_a.id: 1, entry_b.id: 2}}


def test_marks_detail_with_totals_inserts_per_dance_and_per_round_rows():
    # Per user request: after every (round, dance) group, a "Total" row
    # summing that dance's marks; after every round's dances, a further
    # "Round total" row summing the whole round.
    df = pd.DataFrame(
        [
            {"Round": "Round 1", "Dance": "Waltz", "Judge": "A", "Call": "✓", "_round_order": 1, "_numeric": 1, "_is_placement": False},
            {"Round": "Round 1", "Dance": "Waltz", "Judge": "B", "Call": "--", "_round_order": 1, "_numeric": 0, "_is_placement": False},
            {"Round": "Round 1", "Dance": "Tango", "Judge": "A", "Call": "✓", "_round_order": 1, "_numeric": 1, "_is_placement": False},
            {"Round": "Round 1", "Dance": "Tango", "Judge": "B", "Call": "✓", "_round_order": 1, "_numeric": 1, "_is_placement": False},
        ]
    )
    result = marks_detail_with_totals(df)
    calls = result["Call"].tolist()

    assert calls == ["✓", "--", "Total: marked 1/2", "✓", "✓", "Total: marked 2/2", "Round total: marked 3/4"]


def test_ordinal():
    assert _ordinal(1) == "1st"
    assert _ordinal(2) == "2nd"
    assert _ordinal(3) == "3rd"
    assert _ordinal(4) == "4th"
    assert _ordinal(11) == "11th"
    assert _ordinal(12) == "12th"
    assert _ordinal(13) == "13th"
    assert _ordinal(21) == "21st"
    assert _ordinal(101) == "101st"
    assert _ordinal(111) == "111th"


def test_not_recalled_couples_show_overall_rank_and_marks_count(tmp_path):
    # Per user request: not-recalled couples should show their overall
    # standing plus how many judges marked them in the round they were
    # eliminated in, e.g. "9th (10 marks)", rather than a bare "not
    # recalled" that hides how close they got.
    engine = init_db(tmp_path / "not_recalled_ranking.sqlite3")
    session = get_session(engine)

    competition = Competition(source="ndca_premier", source_code="1", name="Test Comp")
    session.add(competition)
    session.flush()

    event = CompEvent(competition_id=competition.id, raw_title="Test Event")
    session.add(event)
    session.flush()

    final_round = Round(comp_event_id=event.id, round_type="final", round_order=2)
    semi_round = Round(comp_event_id=event.id, round_type="Semi-Final", round_order=1)
    session.add_all([final_round, semi_round])
    session.flush()

    def make_couple(name_a, name_b, competitor_no):
        leader = Person(display_name=name_a)
        follower = Person(display_name=name_b)
        session.add_all([leader, follower])
        session.flush()
        partnership = Partnership(leader_id=leader.id, follower_id=follower.id, kind="amateur")
        session.add(partnership)
        session.flush()
        entry = Entry(competition_id=competition.id, partnership_id=partnership.id, competitor_no=competitor_no)
        session.add(entry)
        session.flush()
        return entry

    finalist = make_couple("Alice", "Bob", "1")
    session.add(Result(comp_event_id=event.id, entry_id=finalist.id, placement_low=1, placement_high=1, field_size=3))
    session.add(Mark(round_id=final_round.id, entry_id=finalist.id, dance="Waltz", placement=1))

    # More judges marked "Carla" than "Eve" in the semi-final, so Carla
    # should rank ahead of Eve despite neither making the final.
    carla = make_couple("Carla", "Dan", "2")
    session.add(Result(comp_event_id=event.id, entry_id=carla.id, placement_low=None, placement_high=None, field_size=3))
    session.add_all(
        [
            Mark(round_id=semi_round.id, entry_id=carla.id, dance="Waltz", recalled=True),
            Mark(round_id=semi_round.id, entry_id=carla.id, dance="Tango", recalled=True),
        ]
    )

    eve = make_couple("Eve", "Frank", "3")
    session.add(Result(comp_event_id=event.id, entry_id=eve.id, placement_low=None, placement_high=None, field_size=3))
    session.add(Mark(round_id=semi_round.id, entry_id=eve.id, dance="Waltz", recalled=True))

    session.commit()

    df = results_for_comp_event(session, event.id)
    rows = df.set_index("Couple")

    assert rows.loc["Alice & Bob", "Placement"] == "1"
    assert rows.loc["Carla & Dan", "Placement"] == "2nd (2 marks)"
    assert rows.loc["Eve & Frank", "Placement"] == "3rd (1 mark)"


def test_best_results_excludes_not_recalled_rank_and_marks_format():
    # Real crash: result_history_for_partnership started showing not-recalled
    # rows as "9th (10 marks)" instead of the literal string "not recalled",
    # so best_results()'s old `Placement != "not recalled"` filter no longer
    # excluded them -- they slipped into `scored` and crashed trying to
    # .astype(float) a string like "9th (10 marks)".
    df = pd.DataFrame(
        [
            {"Placement": "1"},
            {"Placement": "2.5"},
            {"Placement": "3-4"},
            {"Placement": "9th (10 marks)"},
            {"Placement": "not recalled"},
        ]
    )
    result = best_results(df, n=5)
    assert list(result["Placement"]) == ["1", "2.5", "3-4"]


def _add_result(session, competition, partnership, raw_title, competitor_no="1"):
    entry = Entry(competition_id=competition.id, partnership_id=partnership.id, competitor_no=competitor_no)
    session.add(entry)
    session.flush()
    event = CompEvent(competition_id=competition.id, raw_title=raw_title)
    session.add(event)
    session.flush()
    session.add(Result(comp_event_id=event.id, entry_id=entry.id))
    session.flush()


def test_partner_category_abandoned_person_level_pro_am_status_in_favor_of_event_titles(tmp_path):
    # Real bug: Person.ndca_pro_am_status (an aggregated per-person mode
    # across all registrations) turned out to be an age classifier
    # ('Y'outh/'A'mateur), not a role signal -- a real Pro-Am instructor,
    # Arsenii Moroz, never showed 'P', and his own mode status came out 'A'
    # despite clearly being an instructor. Replaced with a per-partnership
    # scan of the raw NDCA event titles that partnership actually entered:
    # any title carrying a "Pro Am"/"ProAm"/"MxAm"/"Mixed Am" marker is
    # decisive, even when other titles for the same partnership also list
    # "AM/AM" as eligible (NDCA events are often open to several categories
    # at once, with no per-couple field saying which one a given couple
    # registered under -- so Pro-Am/Mixed-Am is treated as the stronger
    # signal). Verified against real data: a real professional's (Umario
    # Diallo) 18 partnerships never land in the amateur bucket, while an
    # instructor's (Arsenii Moroz) 3 user-confirmed genuine competitive
    # partners do.
    engine = init_db(tmp_path / "partner_category.sqlite3")
    session = get_session(engine)

    competition = Competition(source="ndca_premier", source_code="1", name="Test Comp")
    session.add(competition)
    session.flush()

    instructor = Person(display_name="Instructor")
    student = Person(display_name="Student")
    session.add_all([instructor, student])
    session.flush()

    partnership = Partnership(leader_id=instructor.id, follower_id=student.id, kind="pro_am")
    session.add(partnership)
    session.flush()

    _add_result(session, competition, partnership, "ProAm Youth Scholarship Int'l Latin")
    session.commit()

    assert partner_category(session, partnership) == "Instructor-style"


def test_partner_category_treats_combined_title_as_instructor_style(tmp_path):
    # A single NDCA event is often open to multiple eligibility categories
    # at once (e.g. "ProAm, Mixed Am, AmAm Youth Single..."), with no field
    # distinguishing which category a specific couple in that heat actually
    # registered under -- so a partnership whose only titles are a mix of
    # AmAm-eligible *and* Pro-Am/Mixed-Am-eligible events is still bucketed
    # as instructor-style, not competitive: a genuinely peer amateur couple
    # (verified against real data) never has a Pro-Am/Mixed-Am marker in
    # any of its titles at all.
    engine = init_db(tmp_path / "partner_category_combined.sqlite3")
    session = get_session(engine)

    competition = Competition(source="ndca_premier", source_code="1", name="Test Comp")
    session.add(competition)
    session.flush()

    instructor = Person(display_name="Instructor")
    student = Person(display_name="Student")
    session.add_all([instructor, student])
    session.flush()

    partnership = Partnership(leader_id=instructor.id, follower_id=student.id, kind="pro_am")
    session.add(partnership)
    session.flush()

    _add_result(session, competition, partnership, "ProAm, Mixed Am, AmAm Youth Single LG-YH Op. Full Gold Int'l Cha Cha")
    session.commit()

    assert partner_category(session, partnership) == "Instructor-style"


def test_partner_category_treats_two_students_as_a_competitive_pair_not_instructor(tmp_path):
    # Confirmed with the user: a partnership whose event titles only ever
    # carry the "AM/AM" marker, never "Pro Am"/"MxAm", is a genuine
    # competitive amateur couple -- even though each partner may separately
    # be a Pro-Am student in other partnerships (real case: Matvii
    # Artiushenko & Sofia Chubay).
    engine = init_db(tmp_path / "partner_category_peer.sqlite3")
    session = get_session(engine)

    competition = Competition(source="ndca_premier", source_code="1", name="Test Comp")
    session.add(competition)
    session.flush()

    student_a = Person(display_name="Student A")
    student_b = Person(display_name="Student B")
    session.add_all([student_a, student_b])
    session.flush()

    partnership = Partnership(leader_id=student_a.id, follower_id=student_b.id, kind="amateur")
    session.add(partnership)
    session.flush()

    _add_result(session, competition, partnership, "Challenges Closed Bronze P1 AM/AM Int'l Latin (CC,R,J)")
    session.commit()

    assert partner_category(session, partnership) == "Competitive partners"


def test_partner_category_treats_unmarked_multi_dance_as_competitive(tmp_path):
    # Per user direction: an unmarked title that isn't single-dance is
    # assumed competitive rather than left ambiguous, since NDCA doesn't
    # always spell out "AM/AM" on events that are amateur-only by
    # construction (real case: Sofia Chubay & Daniel Saba's "Amateur
    # PreChampionship 4-Dance"/"Amateur Open 4/5-Dance" titles never say
    # "AM/AM" but aren't Pro-Am/Mixed-Am either).
    engine = init_db(tmp_path / "partner_category_unmarked_multi.sqlite3")
    session = get_session(engine)

    competition = Competition(source="ndca_premier", source_code="1", name="Test Comp")
    session.add(competition)
    session.flush()

    leader = Person(display_name="Leader")
    follower = Person(display_name="Follower")
    session.add_all([leader, follower])
    session.flush()

    partnership = Partnership(leader_id=leader.id, follower_id=follower.id, kind="amateur")
    session.add(partnership)
    session.flush()

    _add_result(session, competition, partnership, "Amateur PreChampionship 4-Dance P2 Int'l Latin (CC,S,R,J)")
    _add_result(session, competition, partnership, "Amateur Open 4/5-Dance P2 Int'l Latin (CC,S,R,PD,J)", competitor_no="2")
    session.commit()

    assert partner_category(session, partnership) == "Competitive partners"


def test_partner_category_falls_back_to_other_when_no_titles_at_all(tmp_path):
    # Genuine residual: a partnership with no event titles on record at
    # all (e.g. no Result rows yet) -- neither bucket applies.
    engine = init_db(tmp_path / "partner_category_no_titles.sqlite3")
    session = get_session(engine)

    leader = Person(display_name="Leader")
    follower = Person(display_name="Follower")
    session.add_all([leader, follower])
    session.flush()

    partnership = Partnership(leader_id=leader.id, follower_id=follower.id, kind="amateur")
    session.add(partnership)
    session.commit()

    assert partner_category(session, partnership) == "Other partnerships"


def test_partner_category_treats_all_single_dance_titles_as_instructor_style(tmp_path):
    # Second-tier signal when no title carries an explicit marker: a
    # partnership whose *every* title is an isolated "Single Dance" event
    # (never multi-dance/scholarship/championship) is how a coach runs a
    # beginner Pro-Am student through their first events one dance at a
    # time -- real case: 4 of Arsenii Moroz's unmarked partnerships (Ava
    # Marukhyan, Penelope Moskovyan, Ariana Harutyunyan, Victoria Avanesov)
    # are 100% single-dance, vs. 0% for each of his 4 confirmed genuine
    # competitive partners.
    engine = init_db(tmp_path / "partner_category_single_dance.sqlite3")
    session = get_session(engine)

    competition = Competition(source="ndca_premier", source_code="1", name="Test Comp")
    session.add(competition)
    session.flush()

    instructor = Person(display_name="Instructor")
    student = Person(display_name="Student")
    session.add_all([instructor, student])
    session.flush()

    partnership = Partnership(leader_id=instructor.id, follower_id=student.id, kind="pro_am")
    session.add(partnership)
    session.flush()

    _add_result(session, competition, partnership, "Kids Single Dances mL-T2 Cl. Full Bronze Int'l Cha Cha")
    _add_result(session, competition, partnership, "Kids Single Dances mL-T2 Cl. Full Bronze Int'l Samba", competitor_no="2")
    session.commit()

    assert partner_category(session, partnership) == "Instructor-style"
