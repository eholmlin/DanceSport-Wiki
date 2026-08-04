"""End-to-end check: parse+resolve+load real NDCA Premier fixtures, then
query the result back out -- same shape of check as test_end_to_end_wdsf.py.
"""
from pathlib import Path

from sqlalchemy import select

from dsr.db import get_session, init_db
from dsr.load.ndca import load_comp_event, load_event
from dsr.load.wdsf import load_competition
from dsr.models import CompEvent, Entry, Mark, Partnership, Person, Result, Round
from dsr.parse.ndca import parse_competition, parse_competitor_feed

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "ndca"


def _load_all(session):
    competition_staging = parse_competition((FIXTURES / "compyears_by_cyi_1612.json").read_bytes())
    competition = load_competition(session, competition_staging)

    events = parse_competitor_feed((FIXTURES / "competitor_A251_multi_event.json").read_bytes())
    for event in events:
        load_event(session, competition, event)
    session.commit()
    return competition


def test_ndca_competition_and_single_dance_event_load(tmp_path):
    engine = init_db(tmp_path / "ndca_e2e.sqlite3")
    session = get_session(engine)

    competition = _load_all(session)
    assert competition.name == "Austin Star Ball"
    assert competition.sanctioning_body == "NDCA"

    comp_event_id = session.scalar(
        select(CompEvent.id).where(CompEvent.raw_title == "Single Dance Events L-Sr1 Closed Full Bronze Amer. Waltz")
    )
    assert comp_event_id is not None

    rows = session.execute(
        select(Result, Entry, Partnership).join(Entry, Result.entry_id == Entry.id).join(
            Partnership, Entry.partnership_id == Partnership.id
        ).where(Result.comp_event_id == comp_event_id).order_by(Result.placement_low)
    ).all()
    assert len(rows) == 3
    winner_result, winner_entry, winner_partnership = rows[0]
    assert winner_result.placement_low == 1
    assert winner_entry.competitor_no == "112"
    leader = session.get(Person, winner_partnership.leader_id)
    follower = session.get(Person, winner_partnership.follower_id)
    assert {leader.display_name, follower.display_name} == {"Mary Ann Alberry", "Tobi Kretschmer"}

    round_ids = session.scalars(select(Round.id).where(Round.comp_event_id == comp_event_id)).all()
    marks = session.scalars(select(Mark).where(Mark.round_id.in_(round_ids))).all()
    assert len(marks) == 9  # 3 couples * 3 judges, one Skated final round


def test_load_competition_refreshes_source_updated_at_on_rerun(tmp_path):
    # This is the core mechanism scripts/refresh_ndca.py depends on: calling
    # load_competition again with a newer source_updated_at must update the
    # existing row (not just return it untouched), so a later refresh run can
    # compare against the *current* stored value to detect the next change.
    import datetime as dt

    from dsr.parse.staging import StagingCompetition

    engine = init_db(tmp_path / "ndca_refresh.sqlite3")
    session = get_session(engine)

    first_staging = StagingCompetition(
        source="ndca_premier",
        source_code="9999",
        name="Test Classic",
        start_date=dt.date(2026, 1, 1),
        end_date=dt.date(2026, 1, 1),
        city="Testville",
        country="USA",
        sanctioning_body="NDCA",
        url="https://ndcapremier.com/results/?cyi=9999",
        source_updated_at=dt.datetime(2026, 1, 1, 12, 0, 0),
    )
    competition = load_competition(session, first_staging)
    session.commit()
    assert competition.source_updated_at == dt.datetime(2026, 1, 1, 12, 0, 0)
    original_id = competition.id

    revised_staging = StagingCompetition(
        source="ndca_premier",
        source_code="9999",
        name="Test Classic",
        start_date=dt.date(2026, 1, 1),
        end_date=dt.date(2026, 1, 1),
        city="Testville",
        country="USA",
        sanctioning_body="NDCA",
        url="https://ndcapremier.com/results/?cyi=9999",
        source_updated_at=dt.datetime(2026, 1, 2, 9, 30, 0),  # organizer revised results the next day
    )
    updated = load_competition(session, revised_staging)
    session.commit()

    assert updated.id == original_id  # same row, not a duplicate
    assert updated.source_updated_at == dt.datetime(2026, 1, 2, 9, 30, 0)


def test_ndca_load_is_idempotent_on_rerun(tmp_path):
    engine = init_db(tmp_path / "ndca_idempotent.sqlite3")
    session = get_session(engine)
    _load_all(session)

    def counts(s):
        from dsr.models import CompEvent, Competition

        return {
            model.__tablename__: len(s.scalars(select(model)).all())
            for model in (Competition, CompEvent, Round, Person, Partnership, Entry, Result, Mark)
        }

    first = counts(session)

    session2 = get_session(engine)
    _load_all(session2)
    second = counts(session2)

    assert first == second
