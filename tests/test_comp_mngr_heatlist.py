"""Comp Manager heat-list page parsing and loading (e.g. Ohio Star Ball)."""
import datetime as dt

from dsr.db import get_session, init_db
from dsr.load.comp_mngr_heatlist import (
    CompMngrHeatlistEvent,
    events_to_refresh,
    heatlist_url,
    load_comp_mngr_heatlist,
)
from dsr.models import Competition, ScheduledHeat
from dsr.parse.comp_mngr_heatlist import flip_name, parse_heatlists

PAGE = """<html><body>
<div id="block_menu"><table><tr><td>Abalos, Ella</td><td><input type="button" onclick="x"></td></tr></table></div>
<div id="TABLE_CODE_1" style="visibility:hidden">
<strong>Entries for Abalos, Ella</strong><br>
<br><strong>With Blanco, Michael</strong>
<table border="2">
<tr><td>Session@Time</td><td>Number</td><td>Heat</td><td>Event</td></tr>
<tr><td>Friday Matinee Session (7) - Regency Ballroom@11:55AM Friday</td><td>433</td><td>Heat 776 [Ballroom B]</td><td>L-JR1 Open Gold International Cha Cha</td></tr>
<tr><td>Tuesday Daytime Session (1)@03:05PM Tuesday<br>Later rounds:<br>1@03:19PM Tuesday</td><td>433</td><td>Heat 106 [Ballroom A Semi-final Round]</td><td>L-A2 Closed Silver Cha Cha</td></tr>
<tr><td>Tuesday Daytime Session (1)@04:00PM Tuesday<br>Later rounds:<br>1@04:20PM Tuesday<br>1@04:40PM Tuesday</td><td>433</td><td>Heat 130</td><td>Fall &amp; Winter $ Open</td></tr>
</table>
</div>
<div id="TABLE_CODE_2" style="visibility:hidden">
<strong>Entries for Blanco, Michael</strong><br>
<br><strong>With Abalos, Ella</strong>
<table border="2">
<tr><td>Session@Time</td><td>Number</td><td>Heat</td><td>Event</td></tr>
<tr><td>Friday Matinee Session (7) - Regency Ballroom@11:55AM Friday</td><td>433</td><td>Heat 776 [Ballroom B]</td><td>L-JR1 Open Gold International Cha Cha</td></tr>
</table>
</div>
<div id="TABLE_CODE_3" style="visibility:hidden">
<strong>Entries for Solo, Sam</strong><br>
<table border="2">
<tr><td>Session@Time</td><td>Number</td><td>Heat</td><td>Event</td></tr>
<tr><td>Saturday Evening@07:00PM Saturday</td><td>9</td><td>Heat 900 [Ballroom A Final Round]</td><td>Showcase</td></tr>
</table>
</div>
</body></html>"""

START = dt.date(2026, 11, 17)  # a Tuesday


def test_flip_name_only_flips_a_plain_last_comma_first():
    assert flip_name("Abalos, Ella") == "Ella Abalos"
    assert flip_name("Smith, Jo &amp; Lee") == "Jo & Lee Smith"  # one comma still flips; entities decoded
    assert flip_name("Andrea, Justin, Chris") == "Andrea, Justin, Chris"
    assert flip_name("Cher") == "Cher"


def test_parse_heatlists_reads_couples_dates_rounds_and_dedupes_both_sides():
    heats = parse_heatlists(PAGE, start_date=START)
    by_heat = {h.heat_number: h for h in heats}
    assert sorted(by_heat) == ["106", "130", "776", "900"]  # the couple listed under both people appears once

    cha = by_heat["776"]
    assert (cha.partner_1.name, cha.partner_2.name) == ("Ella Abalos", "Michael Blanco")
    assert cha.scheduled_time == dt.datetime(2026, 11, 20, 11, 55)  # Friday after Tuesday the 17th
    assert (cha.floor, cha.round_name, cha.competitor_no) == ("B", "Final", "433")
    assert cha.session == "Friday Matinee Session (7) - Regency Ballroom"
    assert cha.source_event_id == "776-l-jr1-open-gold-international-cha-cha"  # URL-safe
    assert cha.event_name == "L-JR1 Open Gold International Cha Cha"

    assert by_heat["106"].round_name == "Semi-final"  # named in the bracket; later rounds not loaded
    assert by_heat["106"].scheduled_time == dt.datetime(2026, 11, 17, 15, 5)
    # no round named: two later rounds follow, so this is the first of three
    assert by_heat["130"].round_name == "Quarter-final"
    assert by_heat["130"].floor is None
    assert by_heat["130"].event_name == "Fall & Winter $ Open"  # entity decoded

    solo = by_heat["900"]
    assert solo.partner_2 is None and solo.partner_1.name == "Sam Solo"
    assert solo.round_name == "Final"


def test_parse_heatlists_marks_heats_complete_once_past_on_the_venue_clock():
    now = dt.datetime(2026, 11, 17, 15, 30)  # Tuesday 3:30 PM at the venue
    heats = {h.heat_number: h for h in parse_heatlists(PAGE, start_date=START, now_local=now)}
    assert heats["106"].is_complete is True      # 3:05 PM + 15 min < 3:30 PM
    assert heats["130"].is_complete is False     # 4:00 PM is still to come
    assert heats["776"].is_complete is False
    assert not any(h.is_complete for h in parse_heatlists(PAGE, start_date=START))  # no clock, no guess


def test_events_to_refresh_covers_current_and_coming_events_only():
    event = CompMngrHeatlistEvent("x2026", "X", START, dt.date(2026, 11, 22), "Columbus", "America/New_York")
    assert events_to_refresh(dt.date(2026, 11, 19), [event]) == [event]  # in progress
    assert events_to_refresh(dt.date(2026, 11, 1), [event]) == [event]   # within 30 days
    assert events_to_refresh(dt.date(2026, 10, 1), [event]) == []        # too early to bother
    assert events_to_refresh(dt.date(2026, 11, 23), [event]) == []       # over
    assert heatlist_url(event) == "http://www.comp-mngr.com/x2026/x2026_HeatLists.htm"


class _FakeResponse:
    status_code = 200
    content = PAGE.encode()


class _FakeFetcher:
    def get(self, url):
        return _FakeResponse()


def test_load_comp_mngr_heatlist_creates_the_competition_and_is_idempotent(tmp_path):
    session = get_session(init_db(tmp_path / "cm_heats.sqlite3"))
    event = CompMngrHeatlistEvent("ohiotest2026", "Ohio Test Ball", START, dt.date(2026, 11, 22), "Columbus", "America/New_York")
    now = dt.datetime(2026, 11, 17, 21, 0, tzinfo=dt.timezone.utc)  # 4:00 PM Eastern

    assert load_comp_mngr_heatlist(session, _FakeFetcher(), event, now_utc=now) == 4
    competition = session.query(Competition).one()
    assert (competition.source, competition.source_code, competition.city) == ("comp_mngr", "ohiotest2026", "Columbus")
    assert (competition.start_date, competition.end_date) == (START, dt.date(2026, 11, 22))
    rows = session.query(ScheduledHeat).all()
    assert len(rows) == 4
    assert {r.heat_number: r.is_complete for r in rows} == {"106": True, "130": False, "776": False, "900": False}

    assert load_comp_mngr_heatlist(session, _FakeFetcher(), event, now_utc=now) == 4
    assert session.query(ScheduledHeat).count() == 4  # reloading updates, never duplicates
    assert session.query(Competition).count() == 1
