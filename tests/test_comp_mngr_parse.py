import datetime as dt
from pathlib import Path

import pytest

from dsr.parse.comp_mngr import (
    ParseError,
    parse_competition,
    parse_competition_name,
    parse_directory_listing_dates,
    parse_judges,
    parse_person_directory,
    parse_scoresheets_dat,
)
from dsr.parse.staging import StagingResult

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "comp_mngr"


def load(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def test_parse_judges():
    judges = parse_judges(load("tristate2023_scoresheets_index.htm"))
    assert len(judges) == 25
    assert judges[0].letter == "01"
    assert judges[0].name == "Cathi Nyemchek"
    assert all(j.external_ref is None for j in judges)


def test_parse_judges_rejects_garbage():
    with pytest.raises(ParseError):
        parse_judges(b"<html><body>nothing here</body></html>")


def test_parse_competition_name():
    assert parse_competition_name(load("tristate2023_scoresheets_index.htm")) == "Tri-State DanceSport Championships"


def test_parse_directory_listing_dates_from_file_mtimes():
    # Comp Manager has no explicit competition-date field anywhere --
    # dates are inferred from the Apache directory listing's own file
    # modification timestamps. All of Tri-State's files (including its
    # scoresheets ones) are tightly clustered right on the competition's
    # actual dates.
    start, end = parse_directory_listing_dates(load("tristate2023_dir_listing.htm"))
    assert start == dt.date(2023, 3, 19)
    assert end == dt.date(2023, 3, 19)


def test_parse_directory_listing_dates_excludes_stale_scoresheets_regeneration():
    # Real case: New York Dance Festival 2023's ScoresheetsByPerson page/
    # .dat file are stamped 2023-04-12 -- nearly two months after
    # HeatLists.htm/Heats.htm (2023-02-16/02-26) and after the
    # competition's real date (Feb 23 2023, confirmed via web search when
    # this source was first discovered). Including the scoresheets files
    # in the date range would give a nonsensical ~2-month "competition",
    # so they're excluded -- see parse_directory_listing_dates.
    start, end = parse_directory_listing_dates(load("newyorkdf2023_dir_listing.htm"))
    assert start == dt.date(2023, 2, 16)
    assert end == dt.date(2023, 2, 26)


def test_parse_directory_listing_dates_excludes_non_result_files():
    # Real case: Wisconsin State 2023's directory has dozens of
    # per-studio .efo *registration* files timestamped from 7 weeks
    # before the event to after it (e.g. one from 2023-02-07, when the
    # actual competition ran in late April), plus a .stu config file --
    # neither reflects the competition's actual dates and both are
    # excluded by only considering .htm/.dat files.
    start, end = parse_directory_listing_dates(load("wisconsin2023_dir_listing.htm"))
    assert start == dt.date(2023, 4, 24)
    assert end == dt.date(2023, 4, 30)


def test_parse_directory_listing_dates_excludes_stale_zpa_resave():
    # Real case: United States Dance Championships 2023's directory
    # includes a "MYUSDC2023_Anna.ZPA" project-archive file resaved a
    # full year later (2024-07-31) -- excluded by only considering
    # .htm/.dat files, leaving the tightly-clustered real event dates
    # (2023-08-30 to 2023-09-10) from HeatLists/Heats/Placements/etc.
    start, end = parse_directory_listing_dates(load("myusdc2023_dir_listing.htm"))
    assert start == dt.date(2023, 8, 30)
    assert end == dt.date(2023, 9, 10)


def test_parse_directory_listing_dates_excludes_mass_stale_resave_far_from_heatlists():
    # Real case: Holiday Dance Classic 2023's HeatLists.htm and
    # ScoresheetsByPerson.htm/.dat all cluster on 2023-12-10 (the real
    # event), but CategoryResults.htm, Placements.htm, and 40+
    # ResultsCategory_*.htm files -- all plain .htm files, not caught by
    # either exclusion above -- were mass-resaved on 2024-07-30, seven
    # months later. Anchoring on HeatLists.htm and dropping anything
    # outside _DATE_CLUSTER_WINDOW of it keeps the range to the real
    # event date instead of a nonsensical 7-month span.
    start, end = parse_directory_listing_dates(load("holiday2023_dir_listing.htm"))
    assert start == dt.date(2023, 12, 10)
    assert end == dt.date(2023, 12, 10)


def test_parse_directory_listing_dates_excludes_multiple_stale_clusters():
    # Real case: Holiday Dance Classic 2025 has THREE different stale
    # clusters beyond the HeatLists.htm/Program.htm anchor (Dec 11-14,
    # 2025): Placements.htm/ScoresheetsByPerson.htm/.dat resaved a month
    # later (2026-01-12), and 40+ ResultsCategory_*.htm files resaved two
    # weeks later (2025-12-27) -- both far enough from the anchor to be
    # excluded by the cluster window, unlike the wider window that would
    # admit the two-week-later cluster too.
    start, end = parse_directory_listing_dates(load("holiday2025_dir_listing.htm"))
    assert start == dt.date(2025, 12, 11)
    assert end == dt.date(2025, 12, 14)


def test_parse_competition_assembles_staging_competition():
    comp = parse_competition(
        load("tristate2023_scoresheets_index.htm"),
        load("tristate2023_dir_listing.htm"),
        source_code="tristate2023",
        url="http://www.comp-mngr.com/tristate2023/",
    )
    assert comp.source == "comp_mngr"
    assert comp.name == "Tri-State DanceSport Championships"
    assert comp.start_date == dt.date(2023, 3, 19)
    assert comp.end_date == dt.date(2023, 3, 19)
    assert comp.sanctioning_body == "NDCA"


def test_parse_person_directory_strips_bib_and_covers_bibless_partners():
    # Real case: only ONE partner in a couple gets a bib annotation in
    # this page ("Ablitsova, Olena (191)"), never both -- her partner
    # ("Reyzin, Zoe") has no bib here at all, yet both must be present
    # and their names bib-free, since a raw "(191)" in the name would
    # make the same real person look different across competitions.
    directory = parse_person_directory(load("tristate2023_scoresheets_index.htm"))
    assert len(directory) > 700
    olena = next(ref for pid, ref in directory.items() if ref.name.startswith("Ablitsova"))
    assert olena.name == "Ablitsova, Olena"
    zoe = next(ref for pid, ref in directory.items() if "Reyzin, Zoe" in ref.name)
    assert zoe.name == "Reyzin, Zoe"


def test_parse_scoresheets_dat_recall_round_resolves_full_names():
    # Real case: a regular heat block's own rows only carry last names
    # ("126 Fantauzzi/D'Arpino"); full names have to be cross-referenced
    # from the block's own person-id header line against the directory
    # (see _resolve_couple_names) -- this also exercises the fix for a
    # real bug where the id list's trailing \r (Windows line endings)
    # silently broke matching for the *last* id in every block.
    directory = parse_person_directory(load("tristate2023_scoresheets_index.htm"))
    events, skipped = parse_scoresheets_dat(load("tristate2023_scoresheetsbyperson.dat"), directory)
    assert len(events) > 2000

    event = next(
        e
        for e in events
        if e.raw_title == "L-B1 Full Silver Int'l Cha Cha" and e.ranking.rounds[0].round_type == "Semi-final"
    )
    round_ = event.ranking.rounds[0]
    assert round_.entries_in == 9
    assert round_.recalled_count == 6  # 6 of 9 rows show "Recall" in the fixture

    entry = next(e for e in event.ranking.entries if e.competitor_no == "126")
    assert entry.partner_1.name == "Fantauzzi, Shaula"
    assert entry.partner_2.name == "D'Arpino"  # not in the directory -- falls back to the bare surname

    marks_126 = [m for m in event.marks if m.competitor_no == "126"]
    assert len(marks_126) == 5  # 5 judges on this panel
    assert all(m.recalled is True and m.placement is None for m in marks_126)  # every judge recalled #126
    assert {m.judge_letter for m in marks_126} == {"04", "13", "14", "21", "24"}


def test_parse_scoresheets_dat_final_round_has_skated_placements():
    events, _skipped = parse_scoresheets_dat(load("tristate2023_scoresheetsbyperson.dat"))
    event = next(e for e in events if e.raw_title == "L-A2 Newcomer American Waltz Final")
    round_ = event.ranking.rounds[0]
    assert round_.round_type == "final"
    assert round_.recalled_count is None  # no recall concept for a skated final

    assert event.ranking.results == [
        StagingResult(
            competitor_no="156", placement_low=1, placement_high=1, made_final=True, field_size=1
        )
    ]
    marks_156 = [m for m in event.marks if m.competitor_no == "156"]
    assert len(marks_156) == 5
    assert all(m.placement == 1 and m.recalled is None for m in marks_156)


def test_parse_scoresheets_dat_combined_event_result_has_no_marks():
    # "=Combined Event: Combined event award for: <title>" blocks give an
    # overall multi-dance placement (already full names natively, no
    # directory needed) but no per-judge marks -- the per-dance columns
    # there are each dance's placement, not a judge's mark.
    events, _skipped = parse_scoresheets_dat(load("tristate2023_scoresheetsbyperson.dat"))
    event = next(e for e in events if e.raw_title == "Pro/Am Open 'B' 9-Dance Championships")
    assert event.marks == []
    assert event.ranking.results == [
        StagingResult(
            competitor_no="232", placement_low=1, placement_high=1, made_final=True, field_size=1
        )
    ]
    entry = event.ranking.entries[0]
    assert entry.partner_1.name == "Jennifer Hoffman"
    assert entry.partner_2.name == "Dave Hannigan"


def test_parse_scoresheets_dat_omitted_partner_name_becomes_solo_not_blank_person():
    # Real case: "508 Blazhko/" -- the professional half of a Pro-Am
    # couple is sometimes omitted entirely from the abbreviated name
    # form, leaving nothing after the slash. A prior version kept that
    # as an empty-string StagingPersonRef, which is a serious bug: every
    # such blank name resolves to the same Person via exact name
    # matching, silently merging every omitted instructor across the
    # whole competition onto one Person row (91 different real people,
    # on a real load). partner_2 must be None here, not a blank name.
    events, _skipped = parse_scoresheets_dat(load("tristate2023_scoresheetsbyperson.dat"))
    event = next(e for e in events if e.raw_title == "A-PT2 Bronze Int'l Waltz Final")
    entry = next(e for e in event.ranking.entries if e.competitor_no == "508")
    assert entry.partner_1.name == "Blazhko"
    assert entry.partner_2 is None


def test_parse_scoresheets_dat_multi_dance_final_block_produces_result_and_marks():
    # Real case that motivated adding this shape: Arsenii Moroz & Mikaela
    # Holmlin's "Mixed Amateur 3-dance International Latin" result at
    # Holiday Dance Classic 2025 lived entirely inside one of these
    # self-contained "=Heat N: <title>" blocks (Cha Cha/Samba/Rumba
    # sub-tables + a "Final summary" overall-placement table) -- a shape
    # v1 originally skipped entirely, silently dropping their result.
    events, _skipped = parse_scoresheets_dat(load("holiday2025_multidance_blocks.dat"))
    event = next(e for e in events if e.raw_title == "AC-MLY Mixed Amateur 3-dance International Latin (C/S/R)")
    assert event.ranking.rounds[0].round_type == "final"
    assert event.ranking.rounds[0].entries_in == 3

    assert event.ranking.results == [
        StagingResult(competitor_no="519", placement_low=3, placement_high=3, made_final=True, field_size=3),
        StagingResult(competitor_no="562", placement_low=1, placement_high=1, made_final=True, field_size=3),
        StagingResult(competitor_no="792", placement_low=2, placement_high=2, made_final=True, field_size=3),
    ]
    entry = next(e for e in event.ranking.entries if e.competitor_no == "562")
    assert entry.partner_1.name == "Moroz"
    assert entry.partner_2.name == "Holmlin"

    # 3 dances (bare "Cha Cha"/"Samba"/"Rumba" sub-headers, no "Dance "
    # prefix) x 3 couples x 7 judges (03,22,23,25,28,35,41).
    assert len(event.marks) == 3 * 3 * 7
    assert {m.dance for m in event.marks} == {"Cha Cha", "Samba", "Rumba"}
    marks_562 = [m for m in event.marks if m.competitor_no == "562"]
    assert len(marks_562) == 3 * 7
    assert all(m.recalled is None for m in marks_562)  # skated placements, not a recall round


def test_parse_scoresheets_dat_multi_dance_final_block_strips_countback_suffix_and_skips_rule_section():
    # Real case: a tie resolved via countback shows as "2(R11)" in the
    # Final summary's Result column -- only the leading integer is the
    # placement. The "Rule 11" sub-table (tie-break detail) itself
    # contributes no marks or results -- the Final summary already
    # reflects the resolved placement.
    events, _skipped = parse_scoresheets_dat(load("holiday2025_multidance_blocks.dat"))
    event = next(e for e in events if e.raw_title == "L-B Night Club 2-dance (Hustle/West Coast Swing)")
    assert event.ranking.results == [
        StagingResult(competitor_no="175", placement_low=1, placement_high=1, made_final=True, field_size=3),
        StagingResult(competitor_no="207", placement_low=2, placement_high=2, made_final=True, field_size=3),
        StagingResult(competitor_no="311", placement_low=3, placement_high=3, made_final=True, field_size=3),
    ]
    # "Dance Hustle"/"Dance West Coast Swing" sub-headers this time (the
    # other form seen in the wild) x 3 couples x 5 judges (11,28,30,33,34)
    # -- Rule 11's own 2 rows (bib-only, no partner names) never became
    # entries or marks.
    assert len(event.marks) == 2 * 3 * 5
    assert {m.dance for m in event.marks} == {"Hustle", "West Coast Swing"}


def test_parse_scoresheets_dat_multi_dance_recall_round_stays_unhandled():
    # Real case: the same "=Heat"/"=Pro heat" block prefix is also used
    # for a genuine multi-dance *recall* round (per-judge "R" marks, no
    # skated placement), titled with a " - Quarter-final"/" - Semi-final"
    # suffix same as a regular heat block -- confirmed on real Wisconsin
    # State 2023 data, which has all three rounds of this exact division
    # (Quarter-final, Semi-final, and the true multi-dance final). Only
    # the clean-titled final (no round suffix) should come through --
    # the two round-suffixed recall blocks must stay unhandled (not
    # misread their "R" marks as skated placements) rather than being
    # silently guessed at.
    events, skipped = parse_scoresheets_dat(load("wisconsin2023_scoresheetsbyperson.dat"))
    assert skipped > 0
    matches = [e for e in events if e.raw_title == "L-C Pro/Am Closed Silver Smooth Scholarship (W/T/FT)"]
    assert len(matches) == 1
    assert matches[0].ranking.rounds[0].round_type == "final"


def test_parse_scoresheets_dat_skips_unhandled_shapes_without_failing():
    # Wisconsin's export includes 23 "Solo N: ..." individual-routine
    # blocks -- still unparsed (no known case has needed them yet) -- but
    # one unsupported block must not lose the whole file's worth of
    # regular heats and multi-dance finals, same resilience pattern as
    # dsr.parse.ndca's per-event try/except.
    events, skipped = parse_scoresheets_dat(load("wisconsin2023_scoresheetsbyperson.dat"))
    assert len(events) > 3000
    assert skipped > 0


def test_parse_scoresheets_dat_quarter_final_round_type_seen_in_wild():
    # Confirms the round-suffix vocabulary includes "Quarter-final", not
    # just "Semi-final" -- even though (per a real fixture check) every
    # occurrence in this particular file happens to live inside a
    # skipped "=Heat" block rather than a regular one, so this asserts
    # the *skip* doesn't silently misclassify it as some other round.
    events, _skipped = parse_scoresheets_dat(load("wisconsin2023_scoresheetsbyperson.dat"))
    round_types = {e.ranking.rounds[0].round_type for e in events}
    assert round_types <= {"Quarter-final", "Semi-final", "final"}
