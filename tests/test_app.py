"""Regression tests for app.py query helpers -- separate from the parse/load
pipeline tests, since these bugs live in how the Streamlit app queries
already-loaded data.
"""
import numpy as np
import pandas as pd

from app import (
    _highest_round_labels_for_entries,
    _skating_system_rank,
    marks_detail_for_entry,
    marks_detail_with_totals,
    skating_system_results_for_final,
)

from dsr.db import get_session, init_db
from dsr.models import CompEvent, Competition, Entry, Mark, Partnership, Person, Round


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
