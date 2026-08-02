"""M1-equivalent success test (spec milestone M1): fetch->parse->resolve->load
one real competition end to end, then query the full result list back out.

Uses the saved fixtures rather than the live site (spec: "Parsers are
re-runnable against stored raw documents without re-hitting any source").
"""
from pathlib import Path

from sqlalchemy import select

from dsr.db import get_session, init_db
from dsr.fetch.storage import save_raw_document
from dsr.load.wdsf import load_comp_event, load_competition, load_marks, load_officials, load_ranking_page
from dsr.models import Entry, Partnership, Person, Result, Round
from dsr.parse.wdsf import parse_event_page, parse_final_page, parse_marks_page, parse_officials_page, parse_ranking_page

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "wdsf"
EVENT_URL = "https://worlddancesport.org/Events/Taipei-Chinese-Taipei-01082026-02082026-8668"
COMP_EVENT_SOURCE_CODE = "Open-Taipei-Adult-Latin-66537"


def _run_pipeline_once(session):
    event_html = (FIXTURES / "event_taipei_2026.html").read_bytes()
    save_raw_document(session, source="wdsf", url=EVENT_URL, content=event_html, http_status=200)
    competition_staging, comp_event_refs = parse_event_page(event_html, EVENT_URL)
    competition = load_competition(session, competition_staging)

    ref = next(r for r in comp_event_refs if r.source_code == COMP_EVENT_SOURCE_CODE)
    comp_event = load_comp_event(session, competition, ref)

    ranking_html = (FIXTURES / "ranking_taipei_adult_latin_2026.html").read_bytes()
    ranking_url = f"https://worlddancesport.org{ref.ranking_url}"
    save_raw_document(session, source="wdsf", url=ranking_url, content=ranking_html, http_status=200)
    ranking_page = parse_ranking_page(ranking_html)
    entry_by_competitor_no = load_ranking_page(session, "wdsf", competition, comp_event, ranking_page)

    officials_html = (FIXTURES / "officials_taipei_adult_latin_2026.html").read_bytes()
    officials = parse_officials_page(officials_html)
    letter_to_person_id = load_officials(session, "wdsf", officials)

    recall_marks = parse_marks_page((FIXTURES / "marks_taipei_adult_latin_2026.html").read_bytes())
    final_marks = parse_final_page((FIXTURES / "final_taipei_adult_latin_2026.html").read_bytes())
    final_round_order = max(r.round_order for r in ranking_page.rounds)

    n_recall = load_marks(
        session, comp_event, entry_by_competitor_no, letter_to_person_id, recall_marks, final_round_order=None
    )
    n_final = load_marks(
        session,
        comp_event,
        entry_by_competitor_no,
        letter_to_person_id,
        final_marks,
        final_round_order=final_round_order,
    )
    session.commit()
    return competition, comp_event, n_recall, n_final


def test_full_pipeline_one_competition(tmp_path):
    engine = init_db(tmp_path / "e2e.sqlite3")
    session = get_session(engine)

    competition, comp_event, n_recall, n_final = _run_pipeline_once(session)

    assert competition.name == "Taipei - Chinese Taipei"
    assert comp_event.raw_title == "WDSF Open Latin Adult"
    assert n_recall == (19 + 12) * 5 * 11
    assert n_final == 6 * 11

    rows = session.execute(
        select(Result, Entry, Partnership)
        .join(Entry, Result.entry_id == Entry.id)
        .join(Partnership, Entry.partnership_id == Partnership.id)
        .where(Result.comp_event_id == comp_event.id)
        .order_by(Result.placement_low)
    ).all()
    assert len(rows) == 19  # 19 couples got a placement; 1 excused couple has none

    top3 = []
    for result, entry, partnership in rows[:3]:
        leader = session.get(Person, partnership.leader_id)
        follower = session.get(Person, partnership.follower_id)
        top3.append((result.placement_low, leader.display_name, follower.display_name, entry.competitor_no))
    assert top3[0] == (1, "Ngoc An", "Le To Uyen", "19")
    assert top3[1] == (2, "Lan Yu Hsiang", "Liu Jing", "34")
    assert top3[2] == (3, "Kim Dong Gyu", "Heo Sejin", "269")

    n_rounds = session.scalar(select(Round).where(Round.comp_event_id == comp_event.id).limit(100))
    assert n_rounds is not None  # at least one round row exists (full check below)
    all_rounds = session.scalars(select(Round).where(Round.comp_event_id == comp_event.id)).all()
    assert len(all_rounds) == 3  # 1.Round, 2.Round, final


def test_pipeline_is_idempotent_on_rerun(tmp_path):
    engine = init_db(tmp_path / "e2e_idempotent.sqlite3")
    session = get_session(engine)

    _run_pipeline_once(session)
    counts_after_first = _table_counts(session)

    session2 = get_session(engine)
    _run_pipeline_once(session2)
    counts_after_second = _table_counts(session2)

    assert counts_after_first == counts_after_second


def _table_counts(session):
    from dsr.models import CompEvent, Competition, Mark, Person, Result, Round

    return {
        "competition": session.scalar(select(Competition)) and len(session.scalars(select(Competition)).all()),
        "comp_event": len(session.scalars(select(CompEvent)).all()),
        "person": len(session.scalars(select(Person)).all()),
        "partnership": len(session.scalars(select(Partnership)).all()),
        "entry": len(session.scalars(select(Entry)).all()),
        "result": len(session.scalars(select(Result)).all()),
        "round": len(session.scalars(select(Round)).all()),
        "mark": len(session.scalars(select(Mark)).all()),
    }
