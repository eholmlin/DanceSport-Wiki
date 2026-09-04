"""Regression tests for app.py query helpers -- separate from the parse/load
pipeline tests, since these bugs live in how the Streamlit app queries
already-loaded data.
"""
import datetime as dt

import numpy as np
import pandas as pd

from app import (
    _classify_titles,
    _highest_round_labels_for_entries,
    _ordinal,
    _skating_system_rank,
    _split_history_by_category,
    best_results,
    marks_detail_for_entry,
    marks_detail_with_totals,
    result_history_for_partnership,
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


def test_results_for_comp_event_leader_follower_links_point_to_the_right_person(tmp_path):
    # The inverse of the dancer page's "View" link: each row in a
    # competition's results table carries "Leader"/"Follower" columns
    # linking back to that exact person's dancer page (see main()'s
    # linked_person_id). A solo entry has no follower at all -- that cell
    # must be None (no link), not a link to a nonexistent person.
    engine = init_db(tmp_path / "partner_links.sqlite3")
    session = get_session(engine)

    competition = Competition(source="ndca_premier", source_code="1", name="Test Comp")
    session.add(competition)
    session.flush()

    event = CompEvent(competition_id=competition.id, raw_title="Test Event")
    session.add(event)
    session.flush()

    leader = Person(display_name="Alice")
    follower = Person(display_name="Bob")
    session.add_all([leader, follower])
    session.flush()
    partnership = Partnership(leader_id=leader.id, follower_id=follower.id, kind="amateur")
    session.add(partnership)
    session.flush()
    entry = Entry(competition_id=competition.id, partnership_id=partnership.id, competitor_no="1")
    session.add(entry)
    session.flush()
    session.add(Result(comp_event_id=event.id, entry_id=entry.id, placement_low=1, placement_high=1, field_size=2))

    soloist = Person(display_name="Cara")
    session.add(soloist)
    session.flush()
    solo_partnership = Partnership(leader_id=soloist.id, follower_id=None, kind="solo")
    session.add(solo_partnership)
    session.flush()
    solo_entry = Entry(competition_id=competition.id, partnership_id=solo_partnership.id, competitor_no="2")
    session.add(solo_entry)
    session.flush()
    session.add(Result(comp_event_id=event.id, entry_id=solo_entry.id, placement_low=2, placement_high=2, field_size=2))

    session.commit()

    df = results_for_comp_event(session, event.id)
    rows = df.set_index("Couple")

    assert rows.loc["Alice & Bob", "Leader"] == f"?person_id={leader.id}"
    assert rows.loc["Alice & Bob", "Follower"] == f"?person_id={follower.id}"
    assert rows.loc["Cara", "Leader"] == f"?person_id={soloist.id}"
    assert rows.loc["Cara", "Follower"] is None


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


def test_classify_titles_abandoned_person_level_pro_am_status_in_favor_of_event_titles():
    # Real bug: Person.ndca_pro_am_status (an aggregated per-person mode
    # across all registrations) turned out to be an age classifier
    # ('Y'outh/'A'mateur), not a role signal -- a real Pro-Am instructor,
    # Arsenii Moroz, never showed 'P', and his own mode status came out 'A'
    # despite clearly being an instructor. Replaced with a scan of the raw
    # NDCA event titles: any title carrying a "Pro Am"/"ProAm"/"MxAm"/
    # "Mixed Am" marker is decisive, even when other titles also list
    # "AM/AM" as eligible (NDCA events are often open to several categories
    # at once, with no per-couple field saying which one a given couple
    # registered under -- so Pro-Am/Mixed-Am is treated as the stronger
    # signal). Verified against real data: a real professional's (Umario
    # Diallo) 18 partnerships never land in the amateur bucket, while an
    # instructor's (Arsenii Moroz) 3 user-confirmed genuine competitive
    # partners do.
    assert _classify_titles(["ProAm Youth Scholarship Int'l Latin"]) == "Instructor-style"


def test_classify_titles_treats_combined_title_with_role_code_as_instructor_style():
    # A single NDCA event is often open to multiple eligibility categories
    # at once (e.g. "ProAm, Mixed Am, AmAm Youth Single..."), with no field
    # distinguishing which category a specific couple in that heat actually
    # registered under. When such a combined-eligibility title ALSO carries
    # a single-role division code (see _ROLE_DIVISION_CODE), the role code
    # alone decides it -- it can't apply to a genuine peer amateur couple no
    # matter what else the title says.
    assert (
        _classify_titles(["ProAm, Mixed Am, AmAm Youth Single LG-YH Op. Full Gold Int'l Cha Cha"])
        == "Instructor-style"
    )


def test_classify_titles_treats_combined_title_without_role_code_as_competitive():
    # Real bug: a combined-eligibility title with no role code says nothing
    # couple-specific -- it's NDCA bundling every category into one heat
    # (events too small to split by category), not evidence this couple is
    # Pro-Am. Dmitry Dragunov & Michelle Bogomolny's multi-year, AM/AM-only
    # Championship history (including a U.S. National title) had a chunk of
    # its "Single" results wrongly bucketed as Instructor-style purely
    # because those combined-eligibility titles also mentioned "Mixed Am".
    # Fixed by only trusting Pro-Am/Mixed-Am wording as decisive when the
    # same title doesn't ALSO list AmAm as eligible.
    assert (
        _classify_titles(["ProAm, Mixed Am, AmAm Youth Single AC-TB Cl. PreBronze Int'l Cha Cha"])
        == "Competitive partners"
    )


def test_classify_titles_treats_amam_marker_as_competitive():
    # Confirmed with the user: a title that only ever carries the "AM/AM"
    # marker, never "Pro Am"/"MxAm", is a genuine competitive amateur
    # couple -- even though each partner may separately be a Pro-Am student
    # in other partnerships (real case: Matvii Artiushenko & Sofia Chubay).
    assert _classify_titles(["Challenges Closed Bronze P1 AM/AM Int'l Latin (CC,R,J)"]) == "Competitive partners"


def test_classify_titles_treats_unmarked_multi_dance_as_competitive():
    # Per user direction: an unmarked title that isn't single-dance is
    # assumed competitive rather than left ambiguous, since NDCA doesn't
    # always spell out "AM/AM" on events that are amateur-only by
    # construction (real case: Sofia Chubay & Daniel Saba's "Amateur
    # PreChampionship 4-Dance"/"Amateur Open 4/5-Dance" titles never say
    # "AM/AM" but aren't Pro-Am/Mixed-Am either).
    assert (
        _classify_titles(
            ["Amateur PreChampionship 4-Dance P2 Int'l Latin (CC,S,R,J)", "Amateur Open 4/5-Dance P2 Int'l Latin (CC,S,R,PD,J)"]
        )
        == "Competitive partners"
    )


def test_classify_titles_falls_back_to_other_when_no_titles_at_all():
    # Genuine residual: no event titles at all (e.g. no Result rows yet)
    # -- neither bucket applies.
    assert _classify_titles([]) == "Other partnerships"


def test_classify_titles_treats_all_single_dance_titles_as_instructor_style():
    # Second-tier signal when no title carries an explicit marker: titles
    # that are all isolated "Single Dance" events (never multi-dance/
    # scholarship/championship) is how a coach runs a beginner Pro-Am
    # student through their first events one dance at a time -- real case:
    # 4 of Arsenii Moroz's unmarked partnerships (Ava Marukhyan, Penelope
    # Moskovyan, Ariana Harutyunyan, Victoria Avanesov) are 100%
    # single-dance, vs. 0% for each of his 4 confirmed genuine competitive
    # partners.
    assert (
        _classify_titles(
            [
                "Kids Single Dances mL-T2 Cl. Full Bronze Int'l Cha Cha",
                "Kids Single Dances mL-T2 Cl. Full Bronze Int'l Samba",
            ]
        )
        == "Instructor-style"
    )


def test_classify_titles_treats_hyphen_and_slash_pro_am_spellings_as_instructor_style():
    # "Pro Am"/"ProAm" were already handled -- "Pro-Am" and "Pro/Am" are
    # two more real spelling variants seen in the wild that the plain
    # marker list was missing entirely.
    assert _classify_titles(["Pro-Am 6-Dance Int Style Full Silver C Int'l Ballroom (W)"]) == "Instructor-style"
    assert _classify_titles(["L-M Pro/Am Closed Bronze American Rhythm Championships (C/R/SW)"]) == "Instructor-style"


def test_classify_titles_treats_pa_abbreviation_as_instructor_style():
    # Real case: "PA AA MA 10-Dance International Championship..." is
    # NDCA's own abbreviated form of the same combined-eligibility titles
    # ("ProAm, Mixed Am, AmAm ...") already handled spelled out -- "PA" is
    # matched with a word boundary specifically so it doesn't false-positive
    # on unrelated words that merely contain "pa" (e.g. "Paso Doble").
    assert _classify_titles(["PA AA MA 10-Dance International Championship Open Bronze mA2 Int'l Latin (R)"]) == (
        "Instructor-style"
    )
    assert _classify_titles(["Adult AC-B2 Op. Full Bronze Int'l Paso Doble"]) != "Instructor-style"


def test_classify_titles_treats_ma_abbreviation_as_instructor_style():
    # Real case: a partnership's own titles include both the abbreviated
    # "AC-MLP1 MA-Full Bronze Int. Cha Cha" and the spelled-out "AC-MLP1
    # Mixed Amateur 3-dance International Latin (C/S/R)" for what's
    # clearly the same division -- "MA" is NDCA's abbreviation for "Mixed
    # Am", matched with a word boundary for the same reason as "PA".
    assert _classify_titles(["AC-MLP1 MA-Full Bronze Int. Cha Cha"]) == "Instructor-style"
    assert _classify_titles(["AC-A1 MA - Bronze 2 International Rumba"]) == "Instructor-style"


def test_classify_titles_treats_single_role_division_code_as_instructor_style():
    # Real case that motivated adding this: Arsenii Moroz & Ellen
    # Sarkisyan's "mL-YH Open Full Gold Int'l Cha Cha" carries no Pro-Am/
    # AmAm marker and isn't literally "Single Dance", so it fell through to
    # the default Competitive bucket despite being the same single-dance
    # Pro-Am progression as their other, explicitly-marked titles. A
    # single-role division code ("L-"/"G-"/"mL-"/"mG-"/"LG-", naming only
    # the student's level since the pro has none) is the signal instead.
    assert _classify_titles(["mL-YH Open Full Gold Int'l Cha Cha"]) == "Instructor-style"
    assert _classify_titles(["L-A2 Bronze 1 Am. Hustle"]) == "Instructor-style"


def test_classify_titles_treats_amateur_couple_division_code_as_competitive():
    # Real counter-case that a naive "no marker, no parens -> instructor"
    # rule would have broken: Adyson Cherkas & Yegor Zhukov's (confirmed
    # genuinely competitive) "Best of the Best AC-JR Int'l Cha Cha" -- an
    # amateur-couple dance-off round, not a Pro-Am progression. "AC-"
    # ("Amateur Couple") classifies both partners together as peers, never
    # co-occurs with a single-role code in the same title (confirmed across
    # the whole database), and needs no explicit rule here -- it just falls
    # through to the same default as any other unmarked, non-single-dance,
    # non-role-coded title.
    assert _classify_titles(["Best of the Best AC-JR  Int'l Cha Cha"]) == "Competitive partners"


def test_split_history_by_category_splits_one_partnership_across_both_sections():
    # Real case: Yegor Zhukov & Izzy Luong's first 3 results (Mar-Apr
    # 2024) carry the ProAm/MxAm marker, but every one of their next 54
    # (Oct 2024 onward) is AmAm or unmarked-competitive -- a genuine
    # transition from Pro-Am student to real competitive amateur partner,
    # not a labeling error. Classifying the whole partnership from "any
    # title ever carries an instructor marker" would lock it into
    # Instructor-style for life despite 95% of its actual history being
    # competitive, so each result is classified by its own title instead,
    # and the partnership shows up under both sections.
    df = pd.DataFrame(
        [
            {"Date": dt.date(2024, 3, 28), "Event": "ProAm & Mixed Am Open Gold JR MxAm Int'l Ballroom (W,T,VW,F,Q)"},
            {"Date": dt.date(2024, 4, 27), "Event": "ProAm Open 5-Dance JR MxAm Int'l Ballroom (W,T,VW,F,Q)"},
            {"Date": dt.date(2024, 10, 17), "Event": "AmAm U21 Open Dance Challenge AM/AM U21 Int'l Ballroom (W,T,VW,F,Q)"},
            {"Date": dt.date(2025, 3, 11), "Event": "U.S. National Youth Ballroom Championship  (W,T,VW*,F,Q)"},
        ]
    )
    split = _split_history_by_category(df)
    assert set(split.keys()) == {"Instructor-style", "Competitive partners"}
    assert len(split["Instructor-style"]) == 2
    assert len(split["Competitive partners"]) == 2


def test_split_history_by_category_keeps_empty_partnership_under_other():
    # A partnership with no results on file at all still needs to render
    # (with its "no results" message) rather than vanishing from every
    # section once grouping happens per-result instead of per-partnership.
    df = pd.DataFrame(columns=["Date", "Event"])
    split = _split_history_by_category(df)
    assert set(split.keys()) == {"Other partnerships"}
    assert split["Other partnerships"].empty


def test_split_history_by_category_promotes_same_day_amam_title_to_instructor_style():
    # Real case: Arsenii Moroz & Eliana Rose Ben Dov's "Youth Bronze
    # 3-Dance Open J1 AM/AM Int'l Latin (CC,S,R)" carries only the AM/AM
    # marker on its own, but landed on the same date (The Royal Ball,
    # 2023-03-18) as 5 Pro-Am "Youth Single Dance ... Int'l <dance>"
    # titles covering those exact same 3 dances plus 2 more -- a combined
    # placement derived from dances already scored individually as
    # Pro-Am. NDCA's own title for the combined round doesn't repeat the
    # eligibility wording, so the per-title rule alone can't catch it;
    # the same-day sibling promotes it instead.
    same_day = dt.date(2023, 3, 18)
    df = pd.DataFrame(
        [
            {"Date": same_day, "Event": "Youth Single Dance AC-mLJ2 Open Bronze Int'l Cha Cha"},
            {"Date": same_day, "Event": "Youth Single Dance AC-mLJ2 Open Bronze Int'l Samba"},
            {"Date": same_day, "Event": "Youth Single Dance AC-mLJ2 Open Bronze Int'l Rumba"},
            {"Date": same_day, "Event": "Youth Single Dance AC-mLJ2 Open Bronze Int'l Paso Doble"},
            {"Date": same_day, "Event": "Youth Single Dance AC-mLJ2 Open Bronze Int'l Jive"},
            {"Date": same_day, "Event": "Youth Bronze 3-Dance Open J1 AM/AM Int'l Latin (CC,S,R)"},
        ]
    )
    split = _split_history_by_category(df)
    assert set(split.keys()) == {"Instructor-style"}
    assert len(split["Instructor-style"]) == 6


def test_split_history_by_category_does_not_promote_amam_title_on_a_different_day():
    # The promotion in the test above is deliberately scoped to the SAME
    # competition date -- an AM/AM title on a day with no Instructor-style
    # sibling for this partnership must stay Competitive, otherwise a
    # genuine across-competition category change (Yegor Zhukov & Izzy
    # Luong, see the split test above) could get masked by unrelated
    # Instructor-style history from a different date.
    df = pd.DataFrame(
        [
            {"Date": dt.date(2024, 3, 28), "Event": "ProAm Open 5-Dance JR MxAm Int'l Ballroom (W,T,VW,F,Q)"},
            {"Date": dt.date(2024, 10, 17), "Event": "AmAm U21 Open Dance Challenge AM/AM U21 Int'l Ballroom (W,T,VW,F,Q)"},
        ]
    )
    split = _split_history_by_category(df)
    assert set(split.keys()) == {"Instructor-style", "Competitive partners"}
    assert len(split["Competitive partners"]) == 1


def test_result_history_view_column_links_to_the_exact_competition_and_event(tmp_path):
    # Each row's "View" column carries a relative URL encoding this exact
    # result's competition and event -- read back by main() via
    # st.query_params to jump straight into Competition search on that
    # event instead of making the user re-search for it (see
    # competition_search's linked_competition_id/linked_event_id).
    engine = init_db(tmp_path / "view_link.sqlite3")
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

    event = CompEvent(competition_id=competition.id, raw_title="Test Event")
    session.add(event)
    session.flush()

    session.add(Result(comp_event_id=event.id, entry_id=entry.id, placement_low=1, placement_high=1, field_size=1))
    session.commit()

    df = result_history_for_partnership(session, partnership.id)
    assert df["View"].iloc[0] == f"?competition_id={competition.id}&event_id={event.id}"
