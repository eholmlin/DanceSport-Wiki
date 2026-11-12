"""Tests for the Following page's helpers: next-up, the field in a heat,
scouting, progress, recaps, alerts and the calendar file's extras."""
import datetime as dt

import pandas as pd

from app import (
    competition_recap,
    filter_heats,
    follow_share_url,
    head_to_head_records,
    heat_card_markdown,
    heat_field,
    heats_at_same_time,
    heats_for_followed_people,
    heats_to_ics,
    marks_gap_between,
    marks_share_by_competition,
    new_results_since,
    next_up_for_followed,
    progress_by_competition,
    result_history_for_partnership,
    scouting_report,
    trend_sentence,
)
from dsr.db import get_session, init_db
from dsr.models import CompEvent, Competition, Entry, Mark, Partnership, Person, Result, Round, ScheduledHeat

NOW = dt.datetime(2026, 11, 14, 9, 0)
UTC = dt.timezone.utc


def _heat_row(name, hour, minute=0, danced="No", event="Waltz", competition="Spring Classic", number="1"):
    t = dt.datetime(2026, 11, 14, hour, minute)
    return {
        "Date": t.date(), "Day": "Saturday", "Time": f"{hour}:{minute:02d}", "Competition": competition,
        "Followed": name, "Couple": f"{name} & Partner", "Event": event, "Round": "Final", "Heat #": number,
        "Floor": "A", "Danced": danced, "View event": "", "_scheduled_time": t, "_uid": f"{name}{hour}{minute}",
        "_followed_names": [name], "_competition_id": 1, "_comp_end": dt.date(2026, 11, 15),
    }


# ---- 1. picking a competition / days ----


def test_filter_heats_narrows_by_competition_and_days():
    heats = pd.DataFrame(
        [
            _heat_row("Ann", 9, competition="Spring Classic"),
            _heat_row("Ann", 10, competition="Fall Ball"),
        ]
    )
    heats.loc[1, "Date"] = dt.date(2026, 11, 15)
    assert len(filter_heats(heats, "All competitions")) == 2
    assert filter_heats(heats, "Fall Ball")["Competition"].tolist() == ["Fall Ball"]
    assert filter_heats(heats, None, [dt.date(2026, 11, 14)])["Competition"].tolist() == ["Spring Classic"]
    assert filter_heats(heats, "Fall Ball", [dt.date(2026, 11, 14)]).empty


# ---- 3. next up ----


def test_next_up_picks_each_dancers_next_undanced_heat_with_time_remaining():
    heats = pd.DataFrame(
        [
            _heat_row("Ann", 8, danced="Yes"),                 # already danced
            _heat_row("Ann", 9, 30, event="Tango"),            # in 30 min
            _heat_row("Ann", 11, event="Foxtrot"),
            _heat_row("Bob", 8, 55, event="Rumba"),            # started 5 min ago: still counts
            _heat_row("Cy", 7, event="Jive"),                  # stale (2 h ago, not ticked): skipped
            _heat_row("Cy", 12, 0, event="Samba"),             # in 3 h
            _heat_row("Dee", 8, danced="Yes"),                 # nothing left
        ]
    )
    result = next_up_for_followed(heats, NOW).set_index("Dancer")
    assert result.loc["Ann", "Event"] == "Tango"
    assert result.loc["Ann", "Starts"] == "in 30 min"
    assert result.loc["Bob", "Starts"] == "Started 5 min ago"
    assert result.loc["Cy", "Event"] == "Samba" and result.loc["Cy", "Starts"] == "in 3 h"
    assert result.loc["Dee", "Starts"] == "No more heats on file"
    # soonest first, nothing-left last
    assert result.index.tolist() == ["Bob", "Ann", "Cy", "Dee"]


def test_next_up_falls_back_to_a_heat_with_no_time_and_labels_far_away_days():
    untimed = _heat_row("Ann", 9)
    untimed["_scheduled_time"] = pd.NaT
    later = _heat_row("Bob", 9)
    later["_scheduled_time"] = dt.datetime(2026, 11, 16, 14, 5)
    result = next_up_for_followed(pd.DataFrame([untimed, later]), NOW).set_index("Dancer")
    assert result.loc["Ann", "Starts"] == "Time not published yet"
    assert result.loc["Bob", "Starts"] == "Mon Nov 16, 2:05 PM"
    assert next_up_for_followed(pd.DataFrame(), NOW).empty


# ---- 4. sharing ----


def test_follow_share_url_carries_the_list_in_the_address():
    assert follow_share_url([3, 7], "https://x.example") == "https://x.example/?follow=3,7"
    assert follow_share_url([], "https://x.example/") == "https://x.example/"


# ---- 5. cards ----


def test_heat_card_escapes_markdown_and_math_characters():
    row = _heat_row("Ann", 9, event="$1000 Scholarship_Open *Bronze*")
    card = heat_card_markdown(row)
    assert "\\$1000 Scholarship\\_Open \\*Bronze\\*" in card
    assert card.startswith("**9:00**")
    assert "Heat 1" in card and "Floor A" in card


# ---- calendar: results-check reminder ----


def test_ics_can_add_one_check_results_reminder_per_competition():
    heats = pd.DataFrame([_heat_row("Ann", 17), _heat_row("Ann", 18, minute=30)])
    text = heats_to_ics(heats, 20, now=dt.datetime(2026, 11, 10, tzinfo=UTC), check_results=True, app_url="https://x.example/?follow=1,2").decode()
    assert text.count("BEGIN:VEVENT") == 3  # two heats + one results check
    assert "SUMMARY:Check results - Spring Classic" in text
    assert "DTSTART:20261116T090000" in text  # the morning after the last day (Nov 15)
    assert "UID:results-1@dancesport-wiki" in text
    assert "URL:https://x.example/?follow=1,2" in text  # not escaped: it is a URI
    plain = heats_to_ics(heats, 20, now=dt.datetime(2026, 11, 10, tzinfo=UTC)).decode()
    assert plain.count("BEGIN:VEVENT") == 2


# ---- a shared little database for the rest ----


def _world(tmp_path):
    engine = init_db(tmp_path / "following_world.sqlite3")
    session = get_session(engine)
    comp_a = Competition(
        source="ndca_premier", source_code="A", name="Autumn Open",
        start_date=dt.date(2026, 9, 5), end_date=dt.date(2026, 9, 6),
    )
    comp_b = Competition(
        source="ndca_premier", source_code="B", name="October Ball",
        start_date=dt.date(2026, 10, 10), end_date=dt.date(2026, 10, 11),
        source_updated_at=dt.datetime(2026, 10, 13, 12, 0, tzinfo=UTC),
    )
    comp_c = Competition(
        source="ndca_premier", source_code="C", name="Spring Classic",
        start_date=dt.date(2026, 11, 14), end_date=dt.date(2026, 11, 15),
    )
    people = {n: Person(display_name=n) for n in ("U1", "U2", "R1a", "R1b", "R2a", "R2b", "J1", "J2", "J3")}
    session.add_all([comp_a, comp_b, comp_c, *people.values()])
    session.flush()
    us = Partnership(leader_id=people["U1"].id, follower_id=people["U2"].id, kind="amateur")
    r1 = Partnership(leader_id=people["R1a"].id, follower_id=people["R1b"].id, kind="amateur")
    r2 = Partnership(leader_id=people["R2a"].id, follower_id=people["R2b"].id, kind="amateur")
    session.add_all([us, r1, r2])
    session.flush()
    judges = [people["J1"], people["J2"], people["J3"]]

    entries = {}
    events = {}
    for comp in (comp_a, comp_b):
        events[comp.id] = CompEvent(competition_id=comp.id, raw_title="Open Waltz Tango")
        session.add(events[comp.id])
        for key, partnership, number in (("us", us, "1"), ("r1", r1, "2"), ("r2", r2, "3")):
            entries[(comp.id, key)] = Entry(competition_id=comp.id, partnership_id=partnership.id, competitor_no=number)
            session.add(entries[(comp.id, key)])
    session.flush()

    # Autumn Open: us 3rd, r1 won, r2 went out in the semi-final.
    event_a = events[comp_a.id]
    semi = Round(comp_event_id=event_a.id, round_type="Semi-Final", round_order=1)
    final = Round(comp_event_id=event_a.id, round_type="final", round_order=2)
    session.add_all([semi, final])
    session.flush()
    for key, placement in (("us", 3), ("r1", 1), ("r2", None)):
        session.add(
            Result(
                comp_event_id=event_a.id, entry_id=entries[(comp_a.id, key)].id,
                placement_low=placement, placement_high=placement, field_size=3,
            )
        )
    recalled = {"us": 3, "r1": 6, "r2": 2}  # of 6 judge-dance marks in the semi-final
    for key, wanted in recalled.items():
        n = 0
        for judge in judges:
            for dance in ("Int'l Waltz", "Int'l Tango"):
                session.add(
                    Mark(
                        round_id=semi.id, entry_id=entries[(comp_a.id, key)].id, judge_person_id=judge.id,
                        dance=dance, recalled=n < wanted,
                    )
                )
                n += 1
    for key, placement in (("us", 3), ("r1", 1)):
        for judge in judges:
            for dance in ("Int'l Waltz", "Int'l Tango"):
                session.add(
                    Mark(
                        round_id=final.id, entry_id=entries[(comp_a.id, key)].id, judge_person_id=judge.id,
                        dance=dance, placement=placement,
                    )
                )

    # October Ball: us 2nd, r1 3rd, r2 not recalled; a scoresheet for us only.
    event_b = events[comp_b.id]
    semi_b = Round(comp_event_id=event_b.id, round_type="Semi-Final", round_order=1)
    session.add(semi_b)
    session.flush()
    for key, placement in (("us", 2), ("r1", 3), ("r2", None)):
        session.add(
            Result(
                comp_event_id=event_b.id, entry_id=entries[(comp_b.id, key)].id,
                placement_low=placement, placement_high=placement, field_size=3,
            )
        )
    for dance in ("Int'l Waltz", "Int'l Tango"):
        session.add(
            Mark(round_id=semi_b.id, entry_id=entries[(comp_b.id, "us")].id, judge_person_id=judges[0].id, dance=dance, recalled=True)
        )

    # Spring Classic heat list (not danced yet).
    def heat(partnership, number, event_id, event_name, floor, hour, minute=0, comp_no="1"):
        return ScheduledHeat(
            competition_id=comp_c.id, partnership_id=partnership.id, source="ndca_premier",
            source_event_id=event_id, event_name=event_name, round_name="Final", heat_number=number,
            floor=floor, competitor_no=comp_no, scheduled_time=dt.datetime(2026, 11, 14, hour, minute),
            updated_at=dt.datetime(2026, 11, 1, tzinfo=UTC),
        )

    session.add_all(
        [
            heat(us, "10", "ev1", "Open Waltz", "A", 10, comp_no="12"),
            heat(r1, "10", "ev1", "Open Waltz", "A", 10, comp_no="5"),
            heat(r2, "10", "ev1", "Open Waltz", "A", 10, comp_no="7"),
            heat(r2, "10", "ev3", "Open Waltz Masters", "A", 10, comp_no="7"),  # same heat, other division
            heat(r2, "11", "ev2", "Silver Rumba", "B", 10, comp_no="7"),
        ]
    )
    session.commit()
    return session, people, us, r1, r2, comp_a, comp_b, comp_c, events


# ---- 2. who's on the floor ----


def test_heat_field_is_the_whole_heat_and_same_time_finds_other_floors(tmp_path):
    session, people, us, r1, r2, _, _, comp_c, _ = _world(tmp_path)
    when = dt.datetime(2026, 11, 14, 10, 0, 40)  # seconds don't matter: same clock minute
    field = heat_field(session, comp_c.id, "10", when, "ndca_premier", "ev1", "Final")
    # the selected event's couples first (by competitor number, numerically), then the other division
    assert field["Comp #"].tolist() == ["5", "7", "12", "7"]
    assert field["Event"].tolist() == ["Open Waltz"] * 3 + ["Open Waltz Masters"]
    assert field["_same_event"].tolist() == [True, True, True, False]
    assert field.loc[0, "Partner"] == f"?person_id={people['R1a'].id}&partnership_id={r1.id}"

    # with no heat number on file it falls back to just that event round
    alone = heat_field(session, comp_c.id, None, None, "ndca_premier", "ev1", "Final")
    assert alone["Comp #"].tolist() == ["5", "7", "12"]

    same_time = heats_at_same_time(session, comp_c.id, when, "10")
    assert same_time["Heat #"].tolist() == ["11"]  # not heat 10 itself
    assert same_time.loc[0, "Floor"] == "B" and same_time.loc[0, "Couples"] == 1
    assert heats_at_same_time(session, comp_c.id, None, "10").empty


# ---- 6. scouting ----


def test_head_to_head_and_scouting_report_summarise_recent_form(tmp_path):
    session, people, us, r1, r2, _, _, comp_c, _ = _world(tmp_path)
    records = head_to_head_records(session, us.id, [r1.id, r2.id])
    # r1 won at Autumn (we were behind) and was 3rd to our 2nd at October (we were ahead)
    assert records[r1.id] == {"met": 2, "ahead": 1, "behind": 1, "even": 0}
    # a recalled-to-the-final couple finishes ahead of one that wasn't recalled
    assert records[r2.id] == {"met": 2, "ahead": 2, "behind": 0, "even": 0}

    field = heat_field(session, comp_c.id, "10", dt.datetime(2026, 11, 14, 10, 0), "ndca_premier", "ev1", "Final")
    field = field[field["_same_event"]]  # only the couples in the same event are rivals
    report = scouting_report(session, "test-db", us.id, field, today=dt.date(2026, 11, 12))
    assert report["Couple"].tolist() == ["R1a & R1b", "R2a & R2b"]  # strongest recent form first
    first = report.iloc[0]
    assert (first["Results (12 mo)"], first["Finals"], first["Best"], first["Avg finish"]) == (2, 2, 1, 2.0)
    assert (first["Met before"], first["Ahead of them"], first["Behind them"]) == (2, 1, 1)
    assert "October Ball" in first["Most recent"]
    second = report.iloc[1]
    assert second["Finals"] == 0 and pd.isna(second["Avg finish"])


def test_scouting_handles_a_rival_with_both_finals_and_not_recalled_results(tmp_path):
    # A rival whose history mixes a final with an elimination used to crash the
    # report (a missing finish became NaN and then int(NaN) failed).
    session, people, us, r1, r2, comp_a, comp_b, comp_c, events = _world(tmp_path)
    extra = CompEvent(competition_id=comp_b.id, raw_title="Open Foxtrot")
    session.add(extra)
    session.flush()
    entry = Entry(competition_id=comp_b.id, partnership_id=r1.id, competitor_no="2b")
    session.add(entry)
    session.flush()
    session.add(Result(comp_event_id=extra.id, entry_id=entry.id, placement_low=None, placement_high=None, field_size=3))
    session.commit()

    field = heat_field(session, comp_c.id, "10", dt.datetime(2026, 11, 14, 10, 0), "ndca_premier", "ev1", "Final")
    report = scouting_report(session, "mixed-db", us.id, field[field["_same_event"]], today=dt.date(2026, 11, 12))
    first = report.iloc[0]
    assert first["Couple"] == "R1a & R1b"
    assert (first["Results (12 mo)"], first["Finals"], first["Best"], first["Avg finish"]) == (3, 2, 1, 2.0)


# ---- 7. progress ----


def test_progress_by_competition_and_trend_sentences(tmp_path):
    session, people, us, *_ = _world(tmp_path)
    history = result_history_for_partnership(session, us.id)
    marks = marks_share_by_competition(session, us.id)
    assert marks.set_index("Competition")["Share of marks (%)"].to_dict() == {"Autumn Open": 50.0, "October Ball": 100.0}

    progress = progress_by_competition(history, marks)
    assert progress["Competition"].tolist() == ["Autumn Open", "October Ball"]  # oldest first
    assert progress["Average finish"].tolist() == [3.0, 2.0]
    assert progress["Finals"].tolist() == [1, 1]
    assert "improving" in trend_sentence(progress, "Share of marks (%)", "Marks", True)
    assert "improving" in trend_sentence(progress, "Average finish", "Finish", False)  # lower is better
    assert "slipping" in trend_sentence(progress, "Average finish", "Finish", True)
    assert trend_sentence(progress.iloc[:1], "Average finish", "Finish", False) is None


# ---- 8. who beat us ----


def test_competition_recap_names_who_finished_ahead_and_the_marks_gap(tmp_path):
    session, people, us, r1, r2, comp_a, *_ = _world(tmp_path)
    recap = competition_recap(session, us.id, comp_a.id)
    assert len(recap) == 1
    row = recap.iloc[0]
    assert row["Placement"] == "3" and row["Highest round"] == "Final"
    assert row["Couples ahead"] == 1
    assert row["Closest couple ahead"] == "R1a & R1b (1)"
    # Final: our six placements are 3s (18), theirs are 1s (6): 12 points behind
    assert row["Gap to them"].startswith("12 placement points behind in the Final (18 vs 6)")
    assert row["Biggest gap by dance"] == "Int'l Waltz (6 placement points)"  # plural; singular is "1 placement point"
    # r1 won, so the winner's row has nobody ahead
    winner = competition_recap(session, r1.id, comp_a.id).iloc[0]
    assert winner["Couples ahead"] == 0 and winner["Closest couple ahead"] == "(won it)"


def test_marks_gap_between_handles_marks_rounds_and_missing_scoresheets():
    tab = pd.DataFrame({"_entry_id": [1, 2], "Semi-Final": [3, 6]})
    routine = pd.DataFrame({"_entry_id": [1, 2], "Semi-Final - Waltz": [1, 3], "Semi-Final - Tango": [2, 3]})
    gap, biggest = marks_gap_between(tab, routine, 1, 2, "Semi-Final")
    assert gap == "3 marks behind in the Semi-Final (3 vs 6)"
    assert biggest == "Waltz (2 marks)"
    # more marks than the couple ahead -> says so rather than a negative gap
    flipped, _ = marks_gap_between(tab, routine, 2, 1, "Semi-Final")
    assert flipped.startswith("3 marks ahead in the Semi-Final")
    one_off = pd.DataFrame({"_entry_id": [1, 2], "Final": [5, 4]})
    assert marks_gap_between(one_off, pd.DataFrame(), 1, 2, "Final")[0] == "1 placement point behind in the Final (5 vs 4)"
    assert marks_gap_between(tab, routine, 1, 2, "Final")[0] == "no scoresheet on file"
    assert marks_gap_between(pd.DataFrame(), pd.DataFrame(), 1, 2, "Final")[0] == "no scoresheet on file"


# ---- 9. what's new ----


def test_new_results_since_uses_publish_date_and_flags_scoresheets(tmp_path):
    session, people, us, *_ = _world(tmp_path)
    ids = [people["U1"].id]
    recent = new_results_since(session, ids, dt.date(2026, 10, 1))
    # only October Ball (published Oct 13) -- Autumn Open was published in September
    assert recent["Competition"].tolist() == ["October Ball"]
    assert recent.loc[0, "Placement"] == "2" and recent.loc[0, "Scoresheet"] == "Yes"
    assert recent.loc[0, "Published"] == dt.date(2026, 10, 13)

    both = new_results_since(session, ids, dt.date(2026, 9, 1))
    assert both["Competition"].tolist() == ["October Ball", "Autumn Open"]  # newest first
    assert both.set_index("Competition").loc["Autumn Open", "Scoresheet"] == "Yes"

    assert new_results_since(session, ids, dt.date(2026, 10, 14)).empty
    assert new_results_since(session, [], dt.date(2026, 9, 1)).empty


def test_heats_for_followed_people_carries_what_the_detail_views_need(tmp_path):
    session, people, us, r1, r2, _, _, comp_c, _ = _world(tmp_path)
    heats = heats_for_followed_people(session, [people["U1"].id])
    assert heats.loc[0, "_competition_id"] == comp_c.id
    assert heats.loc[0, "_partnership_id"] == us.id
    assert heats.loc[0, "_source_event_id"] == "ev1"
    assert heats.loc[0, "_followed_names"] == ["U1"]
    assert heats.loc[0, "_comp_end"] == dt.date(2026, 11, 15)
