"""M1 success check (spec): load one real competition, print its full result
list, eyeball against the live page.

Usage: python scripts/print_wdsf_competition.py
"""
from pathlib import Path

from sqlalchemy import select

from dsr.db import get_session, init_db
from dsr.fetch.storage import save_raw_document
from dsr.load.wdsf import load_comp_event, load_competition, load_marks, load_officials, load_ranking_page
from dsr.models import Entry, Partnership, Person, Result
from dsr.parse.wdsf import parse_event_page, parse_final_page, parse_marks_page, parse_officials_page, parse_ranking_page

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "wdsf"
EVENT_URL = "https://worlddancesport.org/Events/Taipei-Chinese-Taipei-01082026-02082026-8668"
COMP_EVENT_SOURCE_CODE = "Open-Taipei-Adult-Latin-66537"


def main() -> None:
    engine = init_db(Path(__file__).resolve().parent.parent / "data" / "dsr.sqlite3")
    session = get_session(engine)

    event_html = (FIXTURES / "event_taipei_2026.html").read_bytes()
    save_raw_document(session, source="wdsf", url=EVENT_URL, content=event_html, http_status=200)
    competition_staging, comp_event_refs = parse_event_page(event_html, EVENT_URL)
    competition = load_competition(session, competition_staging)

    ref = next(r for r in comp_event_refs if r.source_code == COMP_EVENT_SOURCE_CODE)
    comp_event = load_comp_event(session, competition, ref)

    ranking_html = (FIXTURES / "ranking_taipei_adult_latin_2026.html").read_bytes()
    ranking_page = parse_ranking_page(ranking_html)
    entry_by_competitor_no = load_ranking_page(session, "wdsf", competition, comp_event, ranking_page)

    officials = parse_officials_page((FIXTURES / "officials_taipei_adult_latin_2026.html").read_bytes())
    letter_to_person_id = load_officials(session, "wdsf", officials)

    recall_marks = parse_marks_page((FIXTURES / "marks_taipei_adult_latin_2026.html").read_bytes())
    final_marks = parse_final_page((FIXTURES / "final_taipei_adult_latin_2026.html").read_bytes())
    final_round_order = max(r.round_order for r in ranking_page.rounds)
    load_marks(session, comp_event, entry_by_competitor_no, letter_to_person_id, recall_marks, final_round_order=None)
    load_marks(
        session, comp_event, entry_by_competitor_no, letter_to_person_id, final_marks, final_round_order=final_round_order
    )
    session.commit()

    print(f"{competition.name} -- {comp_event.raw_title} ({competition.start_date} to {competition.end_date})\n")
    rows = session.execute(
        select(Result, Entry, Partnership)
        .join(Entry, Result.entry_id == Entry.id)
        .join(Partnership, Entry.partnership_id == Partnership.id)
        .where(Result.comp_event_id == comp_event.id)
        .order_by(Result.placement_low)
    ).all()
    print(f"{'Place':<8}{'Start#':<8}{'Leader':<22}{'Follower':<22}{'Final?'}")
    for result, entry, partnership in rows:
        leader = session.get(Person, partnership.leader_id)
        follower = session.get(Person, partnership.follower_id)
        place = f"{result.placement_low}" if result.placement_low == result.placement_high else (
            f"{result.placement_low}-{result.placement_high}"
        )
        print(f"{place:<8}{entry.competitor_no:<8}{leader.display_name:<22}{follower.display_name:<22}{result.made_final}")

    excused = session.execute(
        select(Entry).where(Entry.competition_id == competition.id, ~Entry.id.in_(select(Result.entry_id)))
    ).scalars().all()
    if excused:
        print("\nExcused (no result):")
        for entry in excused:
            partnership = session.get(Partnership, entry.partnership_id)
            leader = session.get(Person, partnership.leader_id)
            follower = session.get(Person, partnership.follower_id)
            print(f"  {leader.display_name} - {follower.display_name}")


if __name__ == "__main__":
    main()
