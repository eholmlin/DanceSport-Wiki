import datetime as dt

from dsr.db import get_session, init_db
from dsr.load.heatlist import drop_finished_competitions_with_results, load_scheduled_heat
from dsr.load.wdsf import load_competition
from dsr.models import CompEvent, Entry, Partnership, Person, PersonAlias, Result, ScheduledHeat
from dsr.parse.staging import StagingCompetition, StagingPersonRef, StagingScheduledHeat


def _competition(session):
    staging = StagingCompetition(
        source="ndca_premier",
        source_code="712",
        name="Embassy Dance Championships",
        start_date=dt.date(2026, 9, 2),
        end_date=dt.date(2026, 9, 6),
        city=None,
        country="USA",
        sanctioning_body="NDCA",
        url="https://ndcapremier.com/results/?cyi=712",
    )
    return load_competition(session, staging)


def _staged(*, partner_1: str, partner_2: str, source_event_id="2793", round_name="Final", heat="1212"):
    return StagingScheduledHeat(
        source_event_id=source_event_id,
        event_name="Single Dance mL-P1 CL Full Silver Int'l Cha Cha",
        round_name=round_name,
        heat_number=heat,
        session="09",
        floor="B",
        competitor_no="697",
        scheduled_time=dt.datetime(2026, 9, 6, 14, 55, 6),
        is_complete=False,
        partner_1=StagingPersonRef(name=partner_1),
        partner_2=StagingPersonRef(name=partner_2) if partner_2 else None,
    )


def test_load_scheduled_heat_creates_a_row_with_the_expected_fields(tmp_path):
    engine = init_db(tmp_path / "heatlist.sqlite3")
    session = get_session(engine)
    competition = _competition(session)

    row = load_scheduled_heat(session, "ndca_premier", competition, _staged(partner_1="Matvii Artiushenko", partner_2="Renata Levachova"))
    session.commit()

    assert row.competition_id == competition.id
    assert row.heat_number == "1212"
    assert row.floor == "B"
    assert row.scheduled_time == dt.datetime(2026, 9, 6, 14, 55, 6)
    assert row.is_complete is False

    partnership = session.get(Partnership, row.partnership_id)
    leader = session.get(Person, partnership.leader_id)
    follower = session.get(Person, partnership.follower_id)
    assert {leader.display_name, follower.display_name} == {"Matvii Artiushenko", "Renata Levachova"}


def test_load_scheduled_heat_reuses_existing_partnership_in_either_order(tmp_path):
    # Real case the loader has to handle: results loaded a partnership as
    # (leader=A, follower=B); a heat-list fetch that happened to query B
    # first would otherwise resolve_partnership straight into a *second*,
    # duplicate Partnership row for the same real couple in the opposite
    # order. load_scheduled_heat must find the existing one instead.
    engine = init_db(tmp_path / "heatlist_either_order.sqlite3")
    session = get_session(engine)
    competition = _competition(session)

    a = Person(display_name="Person A")
    b = Person(display_name="Person B")
    session.add_all([a, b])
    session.flush()
    existing_partnership = Partnership(leader_id=a.id, follower_id=b.id, kind="amateur")
    session.add(existing_partnership)
    session.flush()
    # resolve_person's fast path matches an exact (raw_name, source) alias --
    # a real prior load would have left these; without them here, the
    # names would fall through to fuzzy matching (or a brand-new Person)
    # instead of testing the either-order Partnership lookup itself.
    session.add_all(
        [
            PersonAlias(person_id=a.id, raw_name="Person A", source="ndca_premier", confidence=1.0, resolved_by="auto"),
            PersonAlias(person_id=b.id, raw_name="Person B", source="ndca_premier", confidence=1.0, resolved_by="auto"),
        ]
    )
    session.commit()

    # Heat-list fetch queried B first, so B is partner_1 here -- the
    # reverse of the existing partnership's leader/follower order.
    row = load_scheduled_heat(session, "ndca_premier", competition, _staged(partner_1="Person B", partner_2="Person A"))
    session.commit()

    assert row.partnership_id == existing_partnership.id
    assert session.query(Partnership).count() == 1  # no duplicate created


def test_load_scheduled_heat_is_idempotent_and_picks_up_a_time_change(tmp_path):
    engine = init_db(tmp_path / "heatlist_idempotent.sqlite3")
    session = get_session(engine)
    competition = _competition(session)

    first = load_scheduled_heat(session, "ndca_premier", competition, _staged(partner_1="A", partner_2="B"))
    session.commit()
    first_id = first.id

    # Same natural key, but the schedule shifted -- e.g. a round moved earlier.
    changed = _staged(partner_1="A", partner_2="B")
    changed.scheduled_time = dt.datetime(2026, 9, 6, 15, 30, 0)
    second = load_scheduled_heat(session, "ndca_premier", competition, changed)
    session.commit()

    assert second.id == first_id  # updated in place, not a new row
    assert session.query(ScheduledHeat).count() == 1
    assert second.scheduled_time == dt.datetime(2026, 9, 6, 15, 30, 0)


def test_load_scheduled_heat_handles_a_solo_entry(tmp_path):
    engine = init_db(tmp_path / "heatlist_solo.sqlite3")
    session = get_session(engine)
    competition = _competition(session)

    row = load_scheduled_heat(session, "ndca_premier", competition, _staged(partner_1="Solo Dancer", partner_2=None))
    session.commit()

    partnership = session.get(Partnership, row.partnership_id)
    assert partnership.kind == "solo"
    assert partnership.follower_id is None


def _add_result(session, competition, entry_partnership_id):
    comp_event = CompEvent(competition_id=competition.id, raw_title="Some Event")
    session.add(comp_event)
    session.flush()
    entry = Entry(competition_id=competition.id, partnership_id=entry_partnership_id)
    session.add(entry)
    session.flush()
    session.add(Result(comp_event_id=comp_event.id, entry_id=entry.id, placement_low=1, placement_high=1))
    session.commit()


def test_drop_finished_competitions_with_results_drops_a_finished_loaded_competition(tmp_path):
    engine = init_db(tmp_path / "heatlist_drop.sqlite3")
    session = get_session(engine)
    competition = _competition(session)  # ends 2026-09-06
    row = load_scheduled_heat(session, "ndca_premier", competition, _staged(partner_1="A", partner_2="B"))
    session.commit()
    _add_result(session, competition, row.partnership_id)

    dropped = drop_finished_competitions_with_results(session, today=dt.date(2026, 9, 8))

    assert dropped == [(competition.name, 1)]
    assert session.query(ScheduledHeat).count() == 0


def test_drop_finished_competitions_with_results_leaves_results_still_pending(tmp_path):
    # A competition can finish days before its results actually load --
    # dropping the heat list the moment it ends, before results are on
    # file, would erase the only schedule data available in the meantime.
    engine = init_db(tmp_path / "heatlist_no_drop_pending.sqlite3")
    session = get_session(engine)
    competition = _competition(session)  # ends 2026-09-06
    load_scheduled_heat(session, "ndca_premier", competition, _staged(partner_1="A", partner_2="B"))
    session.commit()

    dropped = drop_finished_competitions_with_results(session, today=dt.date(2026, 9, 8))

    assert dropped == []
    assert session.query(ScheduledHeat).count() == 1


def test_drop_finished_competitions_with_results_leaves_an_in_progress_competition(tmp_path):
    engine = init_db(tmp_path / "heatlist_no_drop_in_progress.sqlite3")
    session = get_session(engine)
    competition = _competition(session)  # ends 2026-09-06
    row = load_scheduled_heat(session, "ndca_premier", competition, _staged(partner_1="A", partner_2="B"))
    session.commit()
    _add_result(session, competition, row.partnership_id)

    # today is still within the competition's own date range
    dropped = drop_finished_competitions_with_results(session, today=dt.date(2026, 9, 4))

    assert dropped == []
    assert session.query(ScheduledHeat).count() == 1
