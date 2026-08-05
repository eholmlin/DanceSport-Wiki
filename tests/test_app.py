"""Regression tests for app.py query helpers -- separate from the parse/load
pipeline tests, since these bugs live in how the Streamlit app queries
already-loaded data.
"""
from app import _highest_round_labels_for_entries

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

    assert result[(entry.id, event_made_final.id)] == (2, "Final")
    assert result[(entry.id, event_round1_only.id)] == (1, "Round 1")
