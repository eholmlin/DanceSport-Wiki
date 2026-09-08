"""M2 success check (spec): "search a name, get a correct multi-event history."

Run with: streamlit run app.py
"""
from __future__ import annotations

import datetime as dt
import re
from html import escape
from io import BytesIO
from typing import NamedTuple, Optional

import altair as alt
import pandas as pd
import streamlit as st
from reportlab.lib import colors
from reportlab.lib.pagesizes import landscape, letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Image, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from sqlalchemy import func, or_, select

from dsr.db import get_engine, get_session
from dsr.load.wdsf import guess_style
from dsr.models import CompEvent, Competition, Entry, Mark, Partnership, Person, PersonAlias, Result, Round, ScheduledHeat

st.set_page_config(page_title="DanceSport Wiki", layout="wide")


@st.cache_resource
def _engine():
    return get_engine()


def _session():
    # A cached Engine (connection pool) is safe to share across concurrent
    # script reruns; a cached Session is not -- Sessions aren't thread-safe,
    # and Streamlit can run more than one script execution at a time (e.g.
    # rapid interactions triggering overlapping reruns). Caching the Session
    # itself let two reruns share one DBAPI connection concurrently, which
    # hung the whole server with runaway CPU that didn't even respond to
    # SIGTERM -- only a SIGKILL cleared it. A fresh Session per rerun is
    # cheap (no connection opens until the first query) and the standard
    # SQLAlchemy usage pattern anyway.
    return get_session(_engine())


def _db_identity(session) -> str:
    """Stable, hashable stand-in for "which database this session talks
    to" -- passed alongside the underscore-prefixed (unhashed) session to
    every @st.cache_data-cached query below. Without this, two different
    databases that happen to reuse the same primary key (e.g. two
    from-scratch test databases each starting ids at 1, or a local dev DB
    reset to a fresh file) would collide in the cache, since the session
    object itself is deliberately excluded from the cache key -- a real
    bug caught by the test suite reusing partnership id=1 across isolated
    per-test databases within one process."""
    return str(session.get_bind().url)


@st.cache_data(ttl=600)
def search_people(_session, db_identity: str, term: str, limit: int = 25) -> list[Person]:
    # Cached (leading underscore excludes _session from the cache key, per
    # Streamlit convention -- a fresh Session object every rerun would
    # otherwise defeat the cache entirely) since re-searching the same name
    # is common and this is the first of several round trips a search
    # triggers. Safe to cache the returned Person rows themselves: nothing
    # in models.py uses SQLAlchemy relationship() (every association is a
    # plain FK id column resolved manually via session.get), and these
    # read-only paths never call session.commit(), so there's no lazy-load
    # or expire_on_commit pitfall from handing back detached instances.
    term_like = f"%{term}%"
    alias_person_ids = select(PersonAlias.person_id).where(PersonAlias.raw_name.ilike(term_like))
    stmt = (
        select(Person)
        .where(or_(Person.display_name.ilike(term_like), Person.id.in_(alias_person_ids)))
        .order_by(Person.display_name)
        .limit(limit)
    )
    return list(_session.scalars(stmt).all())


@st.cache_data(ttl=600)
def partnerships_for_person(_session, db_identity: str, person_id: int) -> list[Partnership]:
    """Cached -- see search_people's docstring for why this is safe."""
    stmt = select(Partnership).where(
        or_(
            Partnership.leader_id == person_id,
            Partnership.follower_id == person_id,
            Partnership.student_id == person_id,
        )
    )
    return list(_session.scalars(stmt).all())


_rounds_by_comp_event_cache: dict[int, list[Round]] = {}


def _highest_round_labels_for_entries(
    session, entry_comp_events: list[tuple[int, int]]
) -> dict[tuple[int, int], tuple[int, int, str]]:
    """Batched version: one query for every (entry, comp_event) pair at once,
    instead of one query per row. With ~3M marks loaded, the old per-row query
    (a JOIN + ORDER BY + LIMIT 1 called once per result) made any dancer with
    a few hundred results take ages to render -- a real N+1 query bug, not a
    micro-optimization.

    Keyed by (entry_id, comp_event_id), not entry_id alone: NDCA's Entry row
    is (competition, partnership)-scoped, not per-event (see load/wdsf.py
    docstring), so a couple entered in several events at the same competition
    shares one entry_id across all of them. Keying on entry_id alone picked
    up marks from whichever *other* event that entry made the Final in,
    showing "Final" for an event the couple was actually eliminated from in
    Round 1 -- a real cross-event data leak, caught via a user report of
    "highest round: Final" next to placement "not recalled" for the same row.

    Returns (round_id, round_order, label) rather than just label: round_order
    lets callers rank not-recalled couples by how far they got (Semi-Final >
    Quarter-Final > Round N > ... > Round 1, per the spec's "final has the
    highest round_order"); round_id lets callers batch-fetch that couple's
    marks from the exact round they were eliminated in (see
    marks_totals_for_entries_in_round), without a second per-row query.
    """
    if not entry_comp_events:
        return {}
    entry_ids = {eid for eid, _ in entry_comp_events}
    rows = session.execute(
        select(Mark.entry_id, Round.comp_event_id, Round.id, Round.round_order, Round.round_type)
        .join(Round, Mark.round_id == Round.id)
        .where(Mark.entry_id.in_(entry_ids))
        .distinct()
    ).all()
    best: dict[tuple[int, int], tuple[int, int, str]] = {}
    for entry_id, comp_event_id, round_id, round_order, round_type in rows:
        key = (entry_id, comp_event_id)
        if key not in best or round_order > best[key][1]:
            best[key] = (round_id, round_order, round_type)
    return {key: (rid, ro, "Final" if rt == "final" else rt) for key, (rid, ro, rt) in best.items()}


def _highest_round_fallback(session, comp_event_id: int, placement_low: int | None) -> tuple[int | None, int, str]:
    """Used only for the rare entry with no marks on record at all but a
    known placement (shouldn't normally happen for pipeline-loaded data)."""
    if placement_low is None:
        return (None, -1, "unknown")
    if comp_event_id not in _rounds_by_comp_event_cache:
        _rounds_by_comp_event_cache[comp_event_id] = list(
            session.scalars(
                select(Round)
                .where(Round.comp_event_id == comp_event_id, Round.entries_in.is_not(None))
                .order_by(Round.entries_in.asc())
            ).all()
        )
    for round_row in _rounds_by_comp_event_cache[comp_event_id]:
        if round_row.entries_in >= placement_low:
            return (round_row.id, round_row.round_order, "Final" if round_row.round_type == "final" else round_row.round_type)
    return (None, -1, "unknown")


def marks_totals_for_entries_in_round(session, entry_round_ids: set[tuple[int, int]]) -> dict[tuple[int, int], int]:
    """Batched: for each (entry_id, round_id) pair, how many judges marked
    that couple in that round (across every dance) -- used to rank
    not-recalled couples by how close they got, per user request ("the
    couple with the highest number of marks in the semi-final not to be
    recalled" should rank above couples with fewer marks in the same round).

    Keyed by (entry_id, round_id), not entry_id alone, for the same reason
    _highest_round_labels_for_entries is: one entry_id can have marks across
    several rounds/events, and only the specific round each couple was
    actually eliminated in is relevant here.
    """
    if not entry_round_ids:
        return {}
    entry_ids = {eid for eid, _ in entry_round_ids}
    round_ids = {rid for _, rid in entry_round_ids}
    rows = session.execute(
        select(Mark.entry_id, Mark.round_id, Mark.recalled).where(
            Mark.entry_id.in_(entry_ids), Mark.round_id.in_(round_ids)
        )
    ).all()
    totals: dict[tuple[int, int], int] = {}
    for entry_id, round_id, recalled in rows:
        key = (entry_id, round_id)
        if key not in entry_round_ids:
            continue  # this entry's marks in a round that belongs to a *different* requested pair
        totals[key] = totals.get(key, 0) + (1 if recalled else 0)
    return totals


# Shared by every st.dataframe call that renders a result_histories_for_
# partnerships-shaped df (the full history table and its "Best results"
# subset) -- both carry a "View" column of relative "?competition_id=...
# &event_id=..." URLs (see result_histories_for_partnerships), rendered as
# a clickable link instead of a raw query string.
_VIEW_LINK_COLUMN_CONFIG = {"View": st.column_config.LinkColumn("View", display_text="Open ->")}

# The inverse direction: the Competition page's per-event results table
# (see _field_results_for_comp_events) carries a "Partner" column of
# relative "?person_id=..." URLs -- one dancer's own name is already
# shown in "Couple", so the link text stays generic rather than naming a
# dance role. A single link per row (not one per person): it's one
# partnership either way, and landing on either person's own page shows
# this same partnership among their results, so there's no need to pick
# which side of the couple to link -- was "Leader"/"Follower" (two
# columns) until user feedback that dance-role wording didn't fit here.
_PARTNER_LINK_COLUMN_CONFIG = {"Partner": st.column_config.LinkColumn("Partner", display_text="View ->")}

# The Heat Lists page's own "View event" column (see heat_list_for_
# competition) -- a relative "?heat_competition_id=...&heat_event_id=..."
# URL back into this same page, re-filtered to that event's whole field.
_HEAT_EVENT_LINK_COLUMN_CONFIG = {"View event": st.column_config.LinkColumn("View event", display_text="View ->")}

# Heat Lists rows carry both cross-links: "View event" (this same page,
# re-filtered) and "Partner" (out to the Dancer page, see
# _PARTNER_LINK_COLUMN_CONFIG). Passing a key for a column that isn't
# actually present (e.g. "View event" on the single-event view, which
# drops that column as self-referential) is harmless -- st.dataframe
# just ignores config for columns it isn't given.
_HEAT_LIST_LINK_COLUMN_CONFIG = {**_HEAT_EVENT_LINK_COLUMN_CONFIG, **_PARTNER_LINK_COLUMN_CONFIG}


@st.cache_data(ttl=600)
def result_histories_for_partnerships(_session, db_identity: str, partnership_ids: list[int]) -> dict[int, pd.DataFrame]:
    """Batched version of result_history_for_partnership: one query for
    every partnership's results at once instead of one query per
    partnership. Locally (SQLite, ~0 round-trip cost) a per-partnership
    query loop was invisible; against a real network database (Neon) each
    round trip costs ~85ms, so a dancer with dozens of partnerships spent
    ~15s on this alone before batching -- a real N+1-at-the-partnership-
    level bug that only shows up under real network latency, not local
    dev. Also batches _highest_round_labels_for_entries (already itself
    batched, but was still being called once per partnership) and the
    not-recalled results_for_comp_event cache (now shared across every
    partnership on the page, not just within one).

    Cached (see search_people's docstring for why this is safe) since
    re-viewing the same dancer is common, and this is the single most
    expensive query on the page."""
    if not partnership_ids:
        return {}
    stmt = (
        select(Competition, CompEvent, Entry, Result)
        .select_from(Result)
        .join(Entry, Result.entry_id == Entry.id)
        .join(CompEvent, Result.comp_event_id == CompEvent.id)
        .join(Competition, CompEvent.competition_id == Competition.id)
        .where(Entry.partnership_id.in_(partnership_ids))
        .order_by(Competition.start_date.desc())
    )
    result_rows = _session.execute(stmt).all()
    highest_round_by_key = _highest_round_labels_for_entries(
        _session, [(entry.id, comp_event.id) for _, comp_event, entry, _ in result_rows]
    )

    # Not-recalled rows reuse _field_results_for_comp_events' own Placement
    # string (e.g. "9th (10 marks)") rather than recomputing the same
    # field-wide ranking here -- that keeps the dancer view and the
    # competition view permanently in agreement instead of drifting out of
    # sync, and this was the bug: the dancer view still showed a bare "not
    # recalled" after the competition view was changed to show rank +
    # marks. Every distinct not-recalled event across the whole batch is
    # fetched in one shot up front (not one call per event) -- see
    # _field_results_for_comp_events' own docstring for why that mattered.
    not_recalled_event_ids = {
        comp_event.id for _, comp_event, _, result in result_rows if result.placement_low is None
    }
    event_results_cache = _field_results_for_comp_events(_session, list(not_recalled_event_ids))

    rows_by_partnership: dict[int, list[dict]] = {pid: [] for pid in partnership_ids}
    for competition, comp_event, entry, result in result_rows:
        if result.placement_low is None:
            event_df = event_results_cache[comp_event.id]
            match = event_df.loc[event_df["_entry_id"] == entry.id, "Placement"]
            placement = match.iloc[0] if not match.empty else "not recalled"
        elif result.placement_low == result.placement_high:
            placement = str(result.placement_low)
        else:
            placement = f"{result.placement_low}-{result.placement_high}"
        _round_id, _round_order, highest_round = highest_round_by_key.get(
            (entry.id, comp_event.id)
        ) or _highest_round_fallback(_session, comp_event.id, result.placement_low)
        rows_by_partnership[entry.partnership_id].append(
            {
                "Date": competition.start_date,
                "Competition": competition.name,
                "Event": comp_event.raw_title,
                "Style": comp_event.style,
                "Highest round": highest_round,
                "Placement": placement,
                "Field size": result.field_size,
                "Start #": entry.competitor_no,
                # Relative URL (query params only) rather than an absolute
                # one -- resolves against whatever host the app is served
                # from (localhost in dev, the Streamlit Cloud domain in
                # prod) without hardcoding either. Read back by main() via
                # st.query_params to jump straight into Competition search
                # on this exact event, bypassing the name-search step.
                "View": f"?competition_id={competition.id}&event_id={comp_event.id}",
            }
        )
    return {pid: pd.DataFrame(rows) for pid, rows in rows_by_partnership.items()}


def result_history_for_partnership(session, partnership_id: int) -> pd.DataFrame:
    """Single-partnership convenience wrapper -- see result_histories_for_partnerships
    (used by the dancer page, which needs every partnership's history at once)."""
    return result_histories_for_partnerships(session, _db_identity(session), [partnership_id])[partnership_id]


def best_results(df: pd.DataFrame, n: int = 5) -> pd.DataFrame:
    # A genuine finalist placement is purely numeric (optionally a decimal
    # tie like "2.5", optionally a "N-M" range). Not-recalled rows are
    # excluded by NOT matching this pattern -- previously excluded via
    # `!= "not recalled"`, which broke the moment not-recalled rows started
    # showing "9th (10 marks)" instead of the literal string "not recalled":
    # that format slipped past the equality check and crashed trying to
    # parse "9th (10 marks)" as a float.
    is_placement = df["Placement"].str.match(r"^\d+(\.\d+)?(-\d+(\.\d+)?)?$", na=False)
    scored = df[is_placement].copy()
    if scored.empty:
        return scored
    # placements are usually small ints, but NDCA ties can produce a fractional
    # shared rank (e.g. "2.5") -- float, not int, handles both.
    scored["_sort"] = scored["Placement"].str.split("-").str[0].astype(float)
    return scored.sort_values("_sort").drop(columns="_sort").head(n)


def partner_label(session, partnership: Partnership) -> str:
    # No partnership.kind suffix here: that field is a load-time guess
    # ("amateur" for basically everything), not the same signal as
    # _classify_titles' per-event-title classification -- showing both
    # together read as contradictory (e.g. "[amateur]" on a partnership
    # already grouped under "Instructor-style").
    parts = []
    for pid in (partnership.leader_id, partnership.follower_id):
        if pid is not None:
            p = session.get(Person, pid)
            if p is not None:
                parts.append(p.display_name)
    return " & ".join(parts) if parts else "(solo)"


_INSTRUCTOR_TITLE_MARKERS = ("pro am", "proam", "mxam", "mixed am", "pro-am", "pro/am")
_AMATEUR_TITLE_MARKERS = ("am/am", "amam")
# Manual, individually-verified exceptions to the title-based classification
# below -- not a generalizable rule, a deliberate escape hatch for the rare
# case where a partnership's title text is genuinely ambiguous (an
# unhyphenated role code -- see _ROLE_DIVISION_CODE's docstring) but the
# partnership is independently, heavily confirmed as Instructor-style by the
# rest of its own history. Three different general fixes were tried and
# rejected for these two specific partnerships before resorting to this:
# extending _ROLE_DIVISION_CODE to match unhyphenated codes broke on a real
# "Amateur ... AC-mLP1" title elsewhere (Philadelphia Dancesport
# Championships); scoping to this exact competition+date broke on another
# real partnership at that same event with no corroborating evidence either
# way; and cross-referencing the same leader's other partnerships for
# corroborating Pro-Am evidence broke on Lev Libkind, who has an identical
# "confirmed prolific instructor" profile (24 partnerships, 346 of 579
# titles independently Instructor-style) yet has his own explicitly
# "Amateur"-labeled title using the same unhyphenated code shape. Keyed by
# partnership id, not scoped to a date, since both partnerships' entire
# recorded history (as of 2026-09) is consistent with Instructor-style.
_MANUAL_INSTRUCTOR_STYLE_OVERRIDES = {
    25830,  # Arsenii Moroz & Eliana Rose Ben Dov
    24234,  # Arsenii Moroz & Ulyana Saladkova
}
# NDCA's own abbreviations for Pro-Am and Mixed-Am, e.g. "PA AA MA
# 10-Dance..." or "AC-MLP1 MA-Full Bronze Int. Cha Cha" -- the short form
# of the same combined-eligibility titles ("ProAm, Mixed Am, AmAm ...")
# the word markers above already catch (confirmed directly: one real
# partnership's titles include both "AC-MLP1 MA-Full Bronze..." and
# "AC-MLP1 Mixed Amateur 3-dance..." for what's clearly the same
# division). Needs a word-boundary regex rather than a plain substring
# check: "pa"/"ma" lowercased are substrings of unrelated words ("Paso
# Doble") that a bare `in` check would false-positive on. Validated
# against ~17,500 (PA) and ~36,700 (MA) real titles with zero conflicts
# across every confirmed-competitive partnership checked.
_INSTRUCTOR_WORD_MARKER = re.compile(r"\b(?:PA|MA)\b")
# A single-role division code -- "L-"/"G-" (Lady/Gentleman) or their
# "mixed"/combined variants "mL-"/"mG-"/"LG-" -- names only the *student's*
# level in a Pro-Am pairing, since the professional partner has no level of
# their own to classify. Contrasts with "AC-" ("Amateur Couple"), which
# classifies both partners together as peers -- confirmed across the whole
# database (400k+ titles) that these two code families never co-occur in
# the same title. Real case that motivated adding this: Arsenii Moroz &
# Ellen Sarkisyan's "mL-YH Open Full Gold Int'l Cha Cha" carries no
# Pro-Am/AmAm marker and isn't literally "Single Dance", so it fell through
# to the default Competitive bucket despite being the same single-dance
# Pro-Am progression as their other, explicitly-marked titles.
_ROLE_DIVISION_CODE = re.compile(r"\b(?:mL|mG|LG|L|G)-[A-Za-z0-9]")


def _classify_titles(titles: list[str]) -> str:
    """Classify a partnership from the raw_title of every NDCA event it
    actually entered, not from Person.ndca_pro_am_status (abandoned: that
    field is an aggregated per-person mode across all registrations, and
    turned out to be an age classifier ('Y'outh/'A'mateur), not a role
    signal -- a real instructor like Arsenii Moroz never shows 'P').

    NDCA event titles carry an explicit eligibility marker naming which
    categories may enter -- "Pro Am"/"ProAm"/"Pro-Am"/"Pro/Am", "MxAm"/
    "Mixed Am", "AM/AM"/"AmAm", or NDCA's own abbreviated combined-
    eligibility form ("PA AA MA ...", see _INSTRUCTOR_WORD_MARKER) -- but a
    single event/heat is often open to *multiple* categories at once (e.g.
    "ProAm, Mixed Am, AmAm Youth Single..."), with no per-couple field
    distinguishing which category any specific couple in that heat
    actually registered under. So a lone title can't be trusted, and
    signals are checked in this order:

    1. The title carries a single-role division code (see
    _ROLE_DIVISION_CODE) -- names only the *student's* level, so it cannot
    apply to a genuine peer amateur couple no matter what else the title
    says. Checked first and decisive regardless of anything else in the
    same title (verified: zero titles in the whole database combine a role
    code with an AmAm-eligible marker and no Pro-Am/Mixed-Am wording at
    all) -- one such title in an otherwise-unmarked partnership is still
    that same partnership's Pro-Am history.

    2. A Pro-Am/Mixed-Am marker (word or abbreviated form) is decisive --
    but only when the *same* title doesn't also explicitly list AmAm as
    eligible. A combined-eligibility title ("ProAm, Mixed Am, AmAm Youth
    Single...") says nothing couple-specific -- it's NDCA bundling every
    category into one heat for events too small to split, not evidence
    this couple is Pro-Am. Real case that motivated this: Dmitry Dragunov &
    Michelle Bogomolny's multi-year, AM/AM-only Championship history
    (including a U.S. National title) had a chunk of "Single" results
    wrongly bucketed as Instructor-style purely because those
    combined-eligibility titles also happened to say "Mixed Am". Re-checked
    against the two people this whole marker scheme was originally built
    on: of Umario Diallo's and Arsenii Moroz's 260 combined-eligibility
    titles between them, 257 are unaffected (caught by rule 1's role code
    instead) and only 3 of Arsenii's flip to Competitive. The abbreviated
    word-marker search also normalizes "_" to a space first, since NDCA
    sometimes joins words with an underscore instead ("US National PA_MA
    TB_PT CL. Championships...", 60 titles database-wide) -- "_" counts as
    a word character in regex, so \bMA\b never matched "PA_MA" as written.

    3. Otherwise (no role code, no exclusively-Pro-Am/Mixed-Am marker) --
    fall through to two more signals, in order:

       a. AM/AM/AmAm is listed anywhere -- Competitive (this also now
       catches combined-eligibility titles that rule 2 declined to decide,
       e.g. Dmitry's, since AmAm being listed is real, if weak, amateur
       evidence once nothing stronger overrides it).

       b. Every title is an isolated "Single Dance" event (never
       multi-dance/scholarship/championship) -- how a coach runs a
       beginner Pro-Am student through their first events one dance at a
       time (verified: Arsenii Moroz's 4 confirmed competitive partners
       are 0% single-dance each, while 4 of his unmarked partnerships are
       100% single-dance each -- a clean split, no overlap). Intentionally
       narrow (100% single-dance, not just "mostly"): it does NOT
       generalize to every unmarked partnership -- Umario Diallo's own
       unmarked partnerships are mostly *not* single-dance-heavy despite
       him being a confirmed professional, so those fall through to the
       final default instead. This is a whole-partnership aggregate ("every
       title"), and only fires when more than one title is passed in --
       every real call site (_split_history_by_category, the Heat Lists
       "Category" column) passes a single-element list, where "every title"
       would otherwise degenerate to "is this one title single-dance", a
       much weaker per-event signal that wrongly caught youth divisions
       scored dance-by-dance regardless of Pro-Am status (real case: Dmitry
       Dragunov & Michelle Bogomolny's "AC-PT"/"AC-TB" results, some titled
       "Amateur ... Single Dance" outright).

    4. Final default: a real multi-dance/scholarship/championship title
    exists but never carries any marker -- per user direction, assumed
    competitive rather than left ambiguous, since NDCA doesn't always
    bother spelling out "AM/AM" on events that are amateur-only by
    construction (real case: Sofia Chubay & Daniel Saba's "Amateur
    PreChampionship 4-Dance"/"Amateur Open 4/5-Dance" titles never say
    "AM/AM" but are clearly not Pro-Am/Mixed-Am either). A joint "AC-"
    ("Amateur Couple") division code needs no explicit rule of its own --
    it falls through to this same default (or to 3a above, when the title
    happens to also say AmAm), and never collides with rule 1's role codes
    (see _ROLE_DIVISION_CODE).

    Note "mS1"/"mP2"/"mJ2"-style codes are NOT role codes despite the
    superficial resemblance to "mL-"/"mG-" -- they're age/skill brackets
    that show up in plainly amateur-only titles too (e.g. "Amateur Open 4 &
    5 Dance mS1 Int'l Ballroom", no Pro-Am wording at all) -- confirmed by
    checking before adding them to _ROLE_DIVISION_CODE, which would have
    misclassified those.

    "Other partnerships" is now reachable only when a partnership has no
    event titles on record at all.
    """
    if not titles:
        return "Other partnerships"
    lowered = [t.lower() for t in titles]
    # Role-division code checked first, ahead of the marker-word checks below:
    # it's the one signal here that's decisive regardless of what else the
    # same title says, since a single-role level code (naming only the
    # student's level) cannot apply to a genuine peer amateur couple no
    # matter how the title otherwise reads (verified: zero titles in the
    # whole database combine a role code with an amateur marker and no
    # instructor wording).
    if any(_ROLE_DIVISION_CODE.search(t) for t in titles):
        return "Instructor-style"
    # A title that names Pro-Am/Mixed-Am wording is only decisive on its own
    # when it does NOT *also* explicitly list AmAm as eligible -- NDCA
    # commonly bundles all three categories into one combined heat (e.g.
    # "ProAm, Mixed Am, AmAm Youth Single ...") for events too small to
    # split by category, and that phrasing says nothing about which
    # category this specific couple actually registered under. Real case:
    # Dmitry Dragunov & Michelle Bogomolny's multi-year AM/AM-only
    # Championship history (including a U.S. National title) got a chunk of
    # its "Single" results wrongly bucketed as Instructor-style purely
    # because those combined-eligibility titles happened to also mention
    # "Mixed Am". Re-verified against the two people this whole marker
    # scheme was originally built on (Umario Diallo, Arsenii Moroz): of 260
    # combined-eligibility titles across both, 257 are unaffected because
    # they carry a role code too (caught by the check above) -- only 3 of
    # Arsenii's flip to Competitive.
    if any(
        any(marker in t for marker in _INSTRUCTOR_TITLE_MARKERS)
        and not any(am_marker in t for am_marker in _AMATEUR_TITLE_MARKERS)
        for t in lowered
    ):
        return "Instructor-style"
    # "_" is swapped for a space before this search only -- NDCA's own
    # abbreviated combined-eligibility titles sometimes join words with an
    # underscore instead of a space or hyphen (e.g. "US National PA_MA
    # TB_PT CL. Championships..."), and "_" counts as a word character in
    # regex, so \bMA\b never matched "PA_MA" as written. Real case that
    # surfaced this: Eric Groysman & Ella Li's "US National PA_MA TB_PT
    # CL. Championships..." heat at United States Dance Championships
    # showed as Competitive on the Heat Lists page while every other heat
    # for that same partnership (all carrying an explicit "L-P1" role
    # code) showed Instructor-style -- the Heat Lists view has no same-day
    # override like the dancer-search split does, so this one just sat
    # wrong with no way to self-correct. 60 titles database-wide match this
    # exact "US National PA_MA ..." pattern, none also list AmAm.
    if any(
        _INSTRUCTOR_WORD_MARKER.search(title.replace("_", " "))
        and not any(am_marker in t for am_marker in _AMATEUR_TITLE_MARKERS)
        for title, t in zip(titles, lowered)
    ):
        return "Instructor-style"
    if any(marker in t for t in lowered for marker in _AMATEUR_TITLE_MARKERS):
        return "Competitive partners"
    # Guarded to more than one title: this was designed and validated as a
    # whole-partnership aggregate ("every title this partnership ever
    # competed under is single-dance format"), but every real call site in
    # this app passes a single-element list (see _split_history_by_category
    # and the Heat Lists "Category" column), where "all titles" trivially
    # degenerates to "is this one title single-dance" -- a much weaker
    # signal, since plenty of competitions run youth divisions dance-by-
    # dance regardless of Pro-Am status. Real case: Dmitry Dragunov &
    # Michelle Bogomolny's "AC-PT"/"AC-TB" youth single-dance results (some
    # titled "Amateur ... Single Dance" outright) were getting bucketed as
    # Instructor-style purely for being scored one dance at a time, despite
    # a multi-year AM/AM-only Championship history including a U.S.
    # National title with the same partner. Guarding on len(titles) > 1
    # keeps the rule available for its originally-validated use (Arsenii
    # Moroz's 4 confirmed instructor partnerships were 100% single-dance
    # each) without it firing on any real per-row classification.
    if len(titles) > 1 and all("single dance" in t for t in lowered):
        return "Instructor-style"
    return "Competitive partners"


def _has_amateur_marker(title: str) -> bool:
    tl = title.lower()
    return any(marker in tl for marker in _AMATEUR_TITLE_MARKERS)


def _split_history_by_category(df: pd.DataFrame, partnership_id: int | None = None) -> dict[str, pd.DataFrame]:
    """Split one partnership's result history by each individual result's
    own event title, rather than classifying the whole partnership at
    once. A partnership can genuinely change category over time -- real
    case: Yegor Zhukov & Izzy Luong's first 3 titles (Mar-Apr 2024) carry
    the ProAm/MxAm marker, but every one of their next 54 (Oct 2024
    onward) is AmAm or unmarked-competitive. Classifying the whole
    partnership from "any title ever" locked it into Instructor-style
    for life despite 95% of their actual history being a genuine
    competitive partnership -- so the partnership is shown under BOTH
    sections instead, each with only the results that belong there.

    Same marker rules as _classify_titles (each row is classified via a
    single-element list, which exercises exactly the same logic --
    "all single-dance titles" degenerates correctly to "is this one
    title single-dance" for a length-1 list). An empty partnership (no
    results on file yet) stays under "Other partnerships" with its
    empty df, same as before this split existed.

    One more pass after the per-title classification: an AM/AM-marked
    title that shares a competition date with an Instructor-style
    sibling for this same partnership gets promoted to Instructor-style
    too -- NDCA's own combined-round title sometimes doesn't repeat the
    eligibility wording its individual same-day dances carried. Deliberately
    scoped to the SAME competition date (not "ever," like the marker checks
    in _classify_titles) so it can't mask a partnership that genuinely
    changed category between different competitions, e.g. Yegor & Izzy
    above -- a same-day mixture is NDCA's own labeling inconsistency for
    one round, not evidence of a real category change.

    Last pass: partnership_id is checked against
    _MANUAL_INSTRUCTOR_STYLE_OVERRIDES and, if present, every result is
    forced to Instructor-style regardless of title text -- see that
    constant's own docstring for the two partnerships this covers and why
    no general rule works for them instead (real case that motivated it:
    Arsenii Moroz & Eliana Rose Ben Dov's "Youth Bronze 3-Dance Open J1
    AM/AM Int'l Latin (CC,S,R)", The Royal Ball 2023-03-18, used to get
    promoted by the same-day mechanism above via 5 "Youth Single Dance"
    siblings -- but those siblings stopped self-classifying as
    Instructor-style once the per-row single-dance rule was removed, and
    their unhyphenated role code doesn't match _ROLE_DIVISION_CODE either,
    so nothing triggers the promotion for them anymore)."""
    if df.empty:
        return {"Other partnerships": df}
    if partnership_id in _MANUAL_INSTRUCTOR_STYLE_OVERRIDES:
        return {"Instructor-style": df}
    categories = df["Event"].map(lambda title: _classify_titles([title]))
    instructor_dates = set(df.loc[categories == "Instructor-style", "Date"])
    override = (
        (categories == "Competitive partners")
        & df["Event"].map(_has_amateur_marker)
        & df["Date"].isin(instructor_dates)
    )
    categories = categories.mask(override, "Instructor-style")
    return dict(tuple(df.groupby(categories, sort=False)))


def search_competitions(session, term: str, limit: int = 25) -> list[Competition]:
    term_like = f"%{term}%"
    stmt = select(Competition).where(Competition.name.ilike(term_like)).order_by(Competition.start_date.desc()).limit(limit)
    return list(session.scalars(stmt).all())


def events_for_competition(session, competition_id: int) -> list[CompEvent]:
    stmt = (
        select(CompEvent)
        .where(CompEvent.competition_id == competition_id)
        .order_by(CompEvent.style, CompEvent.raw_title)
    )
    return list(session.scalars(stmt).all())


def event_result_counts(session, competition_id: int) -> dict[int, int]:
    """One aggregate query for every event's entry count, instead of one
    per-event query -- a big competition (e.g. Austin Star Ball has ~500
    events) made per-event queries take a minute-plus and rendering all 500
    full result tables at once was heavy enough to bring the whole app down."""
    rows = session.execute(
        select(Result.comp_event_id, func.count())
        .select_from(Result)
        .join(CompEvent, Result.comp_event_id == CompEvent.id)
        .where(CompEvent.competition_id == competition_id)
        .group_by(Result.comp_event_id)
    ).all()
    return dict(rows)


def competitions_with_heatlists(session, term: str | None = None, limit: int = 25) -> list[Competition]:
    """Competitions that have at least one scheduled_heat row -- NDCA
    Premier only for now (see dsr.models.ScheduledHeat), and only ever a
    handful at a time in practice (heat lists are meaningful only for
    upcoming/in-progress competitions), so an empty term lists everything
    on file rather than requiring a name search first, unlike
    search_competitions. Soonest-first ordering, opposite of
    search_competitions' most-recent-first -- an upcoming competition is
    the one someone's about to check a schedule for."""
    stmt = select(Competition).join(ScheduledHeat, ScheduledHeat.competition_id == Competition.id).distinct()
    if term:
        stmt = stmt.where(Competition.name.ilike(f"%{term}%"))
    stmt = stmt.order_by(Competition.start_date).limit(limit)
    return list(session.scalars(stmt).all())


# Every relative-URL cross-link column used anywhere in the app (see
# _VIEW_LINK_COLUMN_CONFIG, _PARTNER_LINK_COLUMN_CONFIG,
# _HEAT_EVENT_LINK_COLUMN_CONFIG below) -- a raw "?person_id=..." query
# string is meaningless outside the app, so _table_pdf_bytes drops
# whichever of these a given table happens to carry.
_LINK_COLUMN_NAMES = {"View", "Partner", "View event"}


def _pdf_cell_str(value) -> str:
    """Blank for a missing value -- None, float NaN, or pandas' own
    nullable-Int64 pd.NA (e.g. a marks-pivot cell for a round a couple
    never reached -- see _labeled_marks_pivot) -- rather than the literal
    "None"/"nan"/"<NA>" str() would produce."""
    if pd.isna(value):
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _pdf_table_flowable(df: pd.DataFrame, cols: list[str] | None = None) -> Table:
    """The styled Table flowable shared by _table_pdf_bytes (one table, one
    PDF) and _event_summary_pdf_bytes (several tables and chart images in
    one PDF) -- extracted so a multi-section report can lay out its own
    Paragraph/Image/PageBreak flowables around each table instead of each
    table insisting on its own document. A table longer than one page
    just spills onto the next page (reportlab's own Table pagination) --
    a two-column same-page layout was tried and dropped as more trouble
    than it was worth (overlapping columns, awkward text wrapping)."""
    cols = [c for c in (cols or df.columns) if c not in _LINK_COLUMN_NAMES]
    data = [cols] + [[_pdf_cell_str(v) for v in row] for row in df[cols].itertuples(index=False, name=None)]
    table = Table(data, repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2c3e50")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTSIZE", (0, 0), (-1, -1), 7),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f2f2f2")]),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ]
        )
    )
    return table


def _table_pdf_bytes(df: pd.DataFrame, title: str) -> bytes:
    """Render any of this app's tables as a landscape PDF, for printing or
    sharing offline (e.g. at the venue, without a laptop)."""
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=landscape(letter), leftMargin=24, rightMargin=24, topMargin=24, bottomMargin=24
    )
    doc.build([Paragraph(title, getSampleStyleSheet()["Heading2"]), Spacer(1, 8), _pdf_table_flowable(df)])
    return buf.getvalue()


# reportlab's Image flowable, given no explicit width/height, renders a PNG
# at one point per pixel -- at _bar_chart_png's 150 dpi that's 1500pt+ wide,
# many times a landscape letter page's ~740pt printable width. _chart_image
# below always sets both explicitly, in the same aspect ratio as
# _bar_chart_png's default figsize, scaled to fit the page.
_CHART_FIGSIZE = (10, 4.5)


def _chart_image(png_bytes: bytes, width: float = 700) -> Image:
    height = width * _CHART_FIGSIZE[1] / _CHART_FIGSIZE[0]
    return Image(BytesIO(png_bytes), width=width, height=height)


def _bar_chart_png(df: pd.DataFrame, value_cols: list[str], figsize: tuple[float, float] = _CHART_FIGSIZE) -> bytes:
    """Grouped bar chart rendered as a PNG (matplotlib), for embedding in
    the event summary PDF report -- reportlab has no native charting, and
    rasterizing the on-screen Altair chart (_grouped_bar_chart) would need
    a headless browser or an extra Vega renderer; matplotlib draws the
    same grouped-bars shape straight from the dataframe, preserving
    df.index's row order (couples) and value_cols' own order (rounds/
    dances) exactly -- the same ordering discipline _grouped_bar_chart
    uses on screen, and for the same reason: nothing here should silently
    re-sort alphabetically."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    categories = [str(c) for c in df.index]
    n_series, n_cats = len(value_cols), len(categories)
    x = np.arange(n_cats)
    bar_width = 0.8 / max(n_series, 1)
    fig, ax = plt.subplots(figsize=figsize)
    max_value = 0
    for i, col in enumerate(value_cols):
        heights = [0 if pd.isna(v) else v for v in df[col]]
        max_value = max(max_value, max(heights, default=0))
        ax.bar(x + i * bar_width - 0.4 + bar_width / 2, heights, width=bar_width, label=col)
    ax.set_xticks(x)
    ax.set_xticklabels(categories, rotation=60, ha="right", fontsize=6)
    ax.tick_params(axis="y", labelsize=7)
    ax.legend(fontsize=7, loc="upper right")
    # 25% headroom above the tallest bar -- per user request, so the legend
    # (upper right) and the bars themselves don't crowd the top edge.
    if max_value > 0:
        ax.set_ylim(0, max_value * 1.25)
    fig.tight_layout()
    buf = BytesIO()
    fig.savefig(buf, format="png", dpi=150)
    plt.close(fig)
    return buf.getvalue()


def _event_summary_pdf_bytes(
    competition: Competition,
    event: CompEvent,
    results_df: pd.DataFrame,
    results_cols: list[str],
    tabulation_df: pd.DataFrame,
    routine_df: pd.DataFrame,
) -> bytes:
    """One PDF covering the whole event, group-level rather than one
    couple at a time: the field results table, the marks tabulation
    (table + chart), and marks by routine and round (one table + chart
    per round) -- per user request for a single downloadable summary
    instead of stitching together several per-table exports by hand."""
    styles = getSampleStyleSheet()
    elements = [
        Paragraph(f"{competition.name} -- {event.raw_title}", styles["Title"]),
        Spacer(1, 12),
        Paragraph("Results", styles["Heading2"]),
        Spacer(1, 6),
        _pdf_table_flowable(results_df, results_cols),
    ]

    if not tabulation_df.empty:
        round_cols = [c for c in tabulation_df.columns if c not in ("Couple", "Placement", "Final")]
        elements += [
            PageBreak(),
            Paragraph("Marks tabulation -- total marks by round", styles["Heading2"]),
            Spacer(1, 6),
            _pdf_table_flowable(tabulation_df),
        ]
        if round_cols:
            png = _bar_chart_png(tabulation_df.set_index("Couple"), round_cols)
            elements += [Spacer(1, 12), _chart_image(png)]

    if not routine_df.empty:
        routine_cols = [c for c in routine_df.columns if c not in ("Couple", "Placement")]
        rounds_present = list(dict.fromkeys(c.split(" - ", 1)[0] for c in routine_cols))
        for round_name in rounds_present:
            this_round_cols = [c for c in routine_cols if c.split(" - ", 1)[0] == round_name]
            elements += [
                PageBreak(),
                Paragraph(f"Marks by routine -- {round_name}", styles["Heading2"]),
                Spacer(1, 6),
                _pdf_table_flowable(routine_df, ["Couple", "Placement"] + this_round_cols),
                Spacer(1, 12),
                _chart_image(_bar_chart_png(routine_df.set_index("Couple"), this_round_cols)),
            ]

    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=landscape(letter), leftMargin=24, rightMargin=24, topMargin=24, bottomMargin=24
    )
    doc.build(elements)
    return buf.getvalue()


def _download_buttons(df: pd.DataFrame, display_cols: list[str], file_stem: str, title: str, container=st) -> None:
    """CSV + PDF download buttons for the same table, side by side. Used
    everywhere a table on this page is worth exporting whole, in place of
    (or alongside) st.dataframe's own per-table "Download as CSV" toolbar
    button, which has no PDF equivalent and can't export a filtered view
    that spans several separately-rendered tables (see heat_list_search)."""
    file_stem = file_stem.replace("/", "-")
    cols = container.columns(2)
    cols[0].download_button(
        "Download as CSV",
        df[display_cols].to_csv(index=False).encode("utf-8"),
        file_name=f"{file_stem}.csv",
        mime="text/csv",
        key=f"csv_{file_stem}",
    )
    cols[1].download_button(
        "Download as PDF",
        _table_pdf_bytes(df[display_cols], title),
        file_name=f"{file_stem}.pdf",
        mime="application/pdf",
        key=f"pdf_{file_stem}",
    )


def _grouped_bar_chart(df: pd.DataFrame, value_cols: list[str]) -> None:
    """Grouped vertical bar chart: one group per df.index value (couples,
    in the dataframe's own row order), one colored bar per value_cols
    entry within each group, in value_cols' own order.

    Built directly with Altair rather than st.bar_chart: passing more
    than one y-column to st.bar_chart re-sorts the color/legend series
    alphabetically regardless of the dataframe's column order, even with
    sort=False (which only controls the x-axis category order) -- e.g.
    "Int'l Cha Cha, Int'l Jive, Int'l Rumba, Int'l Samba" instead of the
    syllabus order "Cha Cha, Samba, Rumba, Jive" (see _DANCE_ORDER), or
    "Quarter-Final, Round 1, Semi-Final" instead of chronological order.
    Explicit `sort=` on both the x-axis and the color/xOffset encodings
    below is what st.bar_chart can't be told to do."""
    x_field = df.index.name or "index"
    long_df = df.reset_index().melt(id_vars=[x_field], value_vars=value_cols, var_name="Series", value_name="Value")
    chart = (
        alt.Chart(long_df)
        .mark_bar()
        .encode(
            x=alt.X(f"{x_field}:N", sort=df.index.tolist(), title=None),
            y=alt.Y("Value:Q", title=None),
            xOffset=alt.XOffset("Series:N", sort=value_cols),
            color=alt.Color("Series:N", sort=value_cols, title=None),
            tooltip=[x_field, "Series", "Value"],
        )
    )
    st.altair_chart(chart, use_container_width=True)


@st.cache_data(ttl=600)
def heat_list_for_competition(_session, db_identity: str, competition_id: int) -> pd.DataFrame:
    """Every scheduled_heat row for one competition, partner names and two
    cross-links joined in via one batched query:

    - "View event": a relative "?heat_competition_id=...&heat_event_id=..."
      URL back into this same page (see main()'s linked_heat_* handling
      and heat_list_search) -- clicking it re-filters to every couple
      entered in that exact event (same source_event_id, every round),
      the "who else is in this division with me" view.
    - "Partner": a relative "?person_id=..." URL out to that couple's own
      Dancer page (see _PARTNER_LINK_COLUMN_CONFIG), so their results at
      other competitions are one click away instead of a fresh name
      search.

    Cached (see search_people's docstring for why this is safe) since a
    competition's heat list can run into the thousands of rows and gets
    reloaded every time a filter widget below changes."""
    rows = _session.execute(
        select(ScheduledHeat, Partnership)
        .join(Partnership, ScheduledHeat.partnership_id == Partnership.id)
        .where(ScheduledHeat.competition_id == competition_id)
    ).all()
    person_ids = {pid for _, p in rows for pid in (p.leader_id, p.follower_id) if pid is not None}
    names = _person_names(_session, person_ids)

    # Field size (how many couples share this exact event+round) needs
    # every row counted first -- same (source_event_id, round_name) key
    # ScheduledHeat's own natural key partly uses, since that's what
    # actually identifies one heat/round instance, not just the event.
    field_sizes: dict[tuple[str, str], int] = {}
    for heat, _partnership in rows:
        key = (heat.source_event_id, heat.round_name)
        field_sizes[key] = field_sizes.get(key, 0) + 1

    out = []
    for heat, partnership in rows:
        parts = [names[pid] for pid in (partnership.leader_id, partnership.follower_id) if pid in names]
        t = heat.scheduled_time
        out.append(
            {
                "Date": t.date() if t else None,
                "Day": t.strftime("%A") if t else None,
                "Time": t.strftime("%I:%M %p").lstrip("0") if t else None,
                "Couple": " & ".join(parts) if parts else "(solo)",
                # Same "?person_id=...&partnership_id=..." cross-link the
                # Competition page's results table uses (see
                # _PARTNER_LINK_COLUMN_CONFIG) -- jumps straight to that
                # exact couple's own results across every competition,
                # not just this one heat list. partnership_id narrows
                # dancer_search to just this partnership (see its own
                # docstring) rather than landing on every partnership
                # this person has ever had. Real request: browsing a heat
                # list and wanting a given couple's prior results
                # elsewhere, with no way to get there short of
                # re-searching their name from scratch and hunting for
                # the right partner among however many they've had.
                "Partner": (
                    f"?person_id={partnership.leader_id if partnership.leader_id is not None else partnership.follower_id}"
                    f"&partnership_id={partnership.id}"
                )
                if partnership.leader_id is not None or partnership.follower_id is not None
                else None,
                "Event": heat.event_name,
                # scheduled_heat has no style column of its own (unlike
                # comp_event, which only exists once results are loaded)
                # -- computed the same way comp_event's is, straight from
                # the title text, see dsr.load.wdsf.guess_style.
                "Style": guess_style(heat.event_name) or "unknown",
                # Same per-title classification as the dancer page's
                # Competitive-partners/Instructor-style split (see
                # _classify_titles/_split_history_by_category) -- reused
                # directly here rather than duplicated, on the exact same
                # signal (the event title itself).
                "Category": _classify_titles([heat.event_name]),
                "Round": heat.round_name,
                "Heat #": heat.heat_number,
                "Field size": field_sizes[(heat.source_event_id, heat.round_name)],
                "Floor": heat.floor,
                "Start #": heat.competitor_no,
                "Danced": "Yes" if heat.is_complete else "No",
                "View event": f"?heat_competition_id={competition_id}&heat_event_id={heat.source_event_id}",
                "_scheduled_time": t,
                "_partnership_id": partnership.id,
                "_source_event_id": heat.source_event_id,
            }
        )
    df = pd.DataFrame(out)
    if not df.empty:
        df = df.sort_values("_scheduled_time", na_position="last").reset_index(drop=True)
    return df


def _person_names(session, person_ids: set[int]) -> dict[int, str]:
    if not person_ids:
        return {}
    rows = session.execute(select(Person.id, Person.display_name).where(Person.id.in_(person_ids))).all()
    return {pid: name for pid, name in rows}


def _format_date_range(start: dt.date, end: dt.date | None) -> str:
    """Friendly "Sep 2-6, 2026" competition date range, in place of the
    literal ISO dates strung together with a hyphen ("2026-09-02 -
    2026-09-06") -- per user feedback that the ISO form was laborious to
    read. Falls back to spelling out both full dates when the months or
    years differ (e.g. "Sep 30 - Oct 2, 2026", "Dec 30, 2026 - Jan 2,
    2027"). date.day is used directly (not %d) since it's already a plain
    int with no leading zero to strip."""
    if end is None or end == start:
        return f"{start.strftime('%b')} {start.day}, {start.year}"
    if start.year == end.year and start.month == end.month:
        return f"{start.strftime('%b')} {start.day}-{end.day}, {start.year}"
    if start.year == end.year:
        return f"{start.strftime('%b')} {start.day} - {end.strftime('%b')} {end.day}, {start.year}"
    return f"{start.strftime('%b')} {start.day}, {start.year} - {end.strftime('%b')} {end.day}, {end.year}"


def _ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


class _PrelimRow(NamedTuple):
    """One couple's not-yet-finalized standing in one comp_event, on the
    way to becoming a row of _field_results_for_comp_events' output df.
    A named tuple rather than a bare one -- a bare positional tuple here
    already caused a real bug once: entry_round_pairs below unpacked its
    last 3 fields via `*_rest, round_id, _ro, entry_id`, which silently
    grabbed the wrong fields the moment leader_id/follower_id were
    appended after entry_id."""

    couple: str
    highest_round: str
    placement: str
    field_size: int
    competitor_no: str
    round_id: Optional[int]
    round_order: int
    entry_id: int
    leader_id: Optional[int]
    follower_id: Optional[int]
    partnership_id: int


def _field_results_for_comp_events(session, comp_event_ids: list[int]) -> dict[int, pd.DataFrame]:
    """Batched core of results_for_comp_event: computes every comp_event's
    full field ranking (each not-recalled couple's "9th (10 marks)" style
    standing needs the *whole field* to compute, not just one couple) for
    many events in one round trip apiece, instead of one round trip apiece
    PER EVENT. results_for_comp_event (single-event, e.g. the competition
    page) and result_histories_for_partnerships (needs many events' not-
    recalled placements at once, to render a dancer's full history) both
    funnel through this -- real case: rendering one dancer's page called
    this once per distinct not-recalled event (18 for a heavy case),
    ~423ms each including its own internal round trips, ~7.6s total
    against a real network database before this batching."""
    if not comp_event_ids:
        return {}
    stmt = (
        select(Result, Entry, Partnership)
        .select_from(Result)
        .join(Entry, Result.entry_id == Entry.id)
        .join(Partnership, Entry.partnership_id == Partnership.id)
        .where(Result.comp_event_id.in_(comp_event_ids))
    )
    rows = session.execute(stmt).all()

    person_ids = {pid for _, _, partnership in rows for pid in (partnership.leader_id, partnership.follower_id) if pid is not None}
    names = _person_names(session, person_ids)
    highest_round_by_key = _highest_round_labels_for_entries(
        session, [(entry.id, result.comp_event_id) for result, entry, _ in rows]
    )

    prelim_by_event: dict[int, list] = {ce_id: [] for ce_id in comp_event_ids}
    for result, entry, partnership in rows:
        parts = [names[pid] for pid in (partnership.leader_id, partnership.follower_id) if pid in names]
        couple = " & ".join(parts) if parts else "(solo)"
        if result.placement_low is None:
            placement = "not recalled"
        elif result.placement_low == result.placement_high:
            placement = str(result.placement_low)
        else:
            placement = f"{result.placement_low}-{result.placement_high}"
        round_id, round_order, highest_round = highest_round_by_key.get(
            (entry.id, result.comp_event_id)
        ) or _highest_round_fallback(session, result.comp_event_id, result.placement_low)
        prelim_by_event[result.comp_event_id].append(
            _PrelimRow(
                couple=couple,
                highest_round=highest_round,
                placement=placement,
                field_size=result.field_size,
                competitor_no=entry.competitor_no,
                round_id=round_id,
                round_order=round_order,
                entry_id=entry.id,
                leader_id=partnership.leader_id,
                follower_id=partnership.follower_id,
                partnership_id=partnership.id,
            )
        )

    # Batch-fetch how many judges marked each not-recalled couple in the
    # specific round they were eliminated in, across every event at once,
    # so they can be ranked by how close they got (see
    # marks_totals_for_entries_in_round) instead of in arbitrary order
    # within the same round.
    entry_round_pairs = {
        (row.entry_id, row.round_id) for prelim in prelim_by_event.values() for row in prelim if row.round_id is not None
    }
    marks_totals = marks_totals_for_entries_in_round(session, entry_round_pairs)

    out_by_event: dict[int, pd.DataFrame] = {}
    for ce_id, prelim in prelim_by_event.items():
        out = []
        for row in prelim:
            out.append(
                {
                    "Couple": row.couple,
                    "Highest round": row.highest_round,
                    "Placement": row.placement,
                    "Field size": row.field_size,
                    "Start #": row.competitor_no,
                    # Relative URL, same convention as result_histories_for_
                    # partnerships' "View" column -- read back by main() via
                    # st.query_params to jump straight to that person's
                    # dancer page, narrowed to just this partnership (see
                    # dancer_search's linked_partnership_id). Leader
                    # preferred, follower as fallback (e.g. a solo entry
                    # has no follower); None only when neither exists, so
                    # the cell renders empty rather than a link to nowhere.
                    "Partner": (
                        f"?person_id={row.leader_id if row.leader_id is not None else row.follower_id}"
                        f"&partnership_id={row.partnership_id}"
                    )
                    if row.leader_id is not None or row.follower_id is not None
                    else None,
                    "_round_order": row.round_order,
                    "_marks_total": marks_totals.get((row.entry_id, row.round_id), 0) if row.round_id is not None else 0,
                    "_first_name": (row.couple.split(" & ")[0].split() or [""])[0],
                    "_entry_id": row.entry_id,
                }
            )
        df = pd.DataFrame(out)
        if not df.empty:
            # Placed couples sort by placement ascending, same as before. Couples
            # who weren't recalled sort after every placed couple; among
            # themselves: by how far they got (Semi-Final before Quarter-Final
            # before Round N before Round N-1, i.e. round_order descending), then
            # by how many judges marked them in that round (descending -- the
            # couple closest to advancing ranks first, e.g. "7th" in a
            # semi-final of 6), then alphabetically by the first (leading)
            # dancer's first name to break any remaining tie. Per user request.
            not_recalled = df["Placement"] == "not recalled"
            df["_group"] = not_recalled.astype(int)
            df["_placement_sort"] = 0.0
            df.loc[~not_recalled, "_placement_sort"] = df.loc[~not_recalled, "Placement"].str.split("-").str[0].astype(float)
            df = df.sort_values(
                ["_group", "_placement_sort", "_round_order", "_marks_total", "_first_name"],
                ascending=[True, True, False, False, True],
            ).reset_index(drop=True)

            # Not-recalled couples show their overall standing (their 1-indexed
            # position in this same field-wide ranking, continuing on from the
            # last finalist) plus how many judges marked them in the round they
            # were eliminated in, e.g. "9th (10 marks)" -- per user request, so
            # "not recalled" alone doesn't hide how close a couple actually got.
            not_recalled = df["Placement"] == "not recalled"
            overall_rank = df.index[not_recalled] + 1
            marks_total = df.loc[not_recalled, "_marks_total"].astype(int)
            df.loc[not_recalled, "Placement"] = [
                f"{_ordinal(rank)} ({marks} mark{'' if marks == 1 else 's'})" for rank, marks in zip(overall_rank, marks_total)
            ]

            # _entry_id is kept (not dropped) so callers can look up which couple a
            # row is, for the judges' marks drill-down -- hidden from display via
            # column_order in st.dataframe rather than dropped here.
            df = df.drop(columns=["_group", "_placement_sort", "_round_order", "_marks_total", "_first_name"])
        out_by_event[ce_id] = df
    return out_by_event


def results_for_comp_event(session, comp_event_id: int) -> pd.DataFrame:
    """Single-event convenience wrapper -- see _field_results_for_comp_events
    (result_histories_for_partnerships needs many events' fields at once)."""
    return _field_results_for_comp_events(session, [comp_event_id])[comp_event_id]


# Standard syllabus order for each style actually seen in this data (matches
# the "W,T,VW,F,Q" / "CC,S,R,PD,J" abbreviations in event titles), not
# alphabetical -- used to order dances in the marks detail and routine-totals
# tables the way a dancer actually expects them. One flat list rather than a
# dict keyed by style: an event only ever uses one style's naming ("Int'l
# ..." or "Amer. ..." throughout), so there's no risk of these interleaving,
# and matching happens by the full "Int'l Waltz"/"Amer. Waltz" phrase (never
# a bare "Waltz"), so a longer phrase like "Int'l Viennese Waltz" is never
# mistaken for a shorter one earlier in the list.
_DANCE_ORDER = [
    # International Standard/Ballroom (W, T, VW, F, Q)
    "Int'l Waltz", "Int'l Tango", "Int'l Viennese Waltz", "Int'l Foxtrot", "Int'l Quickstep",
    # International Latin (CC, S, R, PD, J)
    "Int'l Cha Cha", "Int'l Samba", "Int'l Rumba", "Int'l Paso Doble", "Int'l Jive",
    # American Smooth (W, T, F, VW)
    "Amer. Waltz", "Amer. Tango", "Amer. Foxtrot", "Amer. Viennese Waltz",
    # American Rhythm (CC, R, SW/ECS, B, M)
    "Amer. Cha Cha", "Amer. Rumba", "Amer. Swing", "Amer. Bolero", "Amer. Mambo",
]


def _dance_sort_key(dance_name: str) -> tuple[int, str]:
    name = dance_name or ""
    for i, keyword in enumerate(_DANCE_ORDER):
        if keyword.lower() in name.lower():
            return (i, name)
    return (len(_DANCE_ORDER), name)


def marks_detail_for_entry(session, entry_id: int, comp_event_id: int) -> pd.DataFrame:
    """One row per (round, dance, judge) mark for one couple in one event --
    the raw material for the round/routine/judge totals below.

    Non-final rounds and the skated Final use different judging systems (a
    yes/no mark per judge per dance -- did this judge mark the couple to
    advance -- vs. an ordinal placement per judge per dance), so both "Call"
    (human-readable) and "_numeric" (1/0 for a mark, the placement number for
    Final) are carried through: totals need the numeric form, display needs
    the readable form.

    Terminology: a judge "marks" a couple in a non-final round (that's the
    literal action being recorded -- Mark.recalled is this judge's individual
    yes/no vote). "Recalled" describes the round-level *outcome* once every
    judge's marks are tallied against the panel's callback quota -- a couple
    is recalled or not, but no single judge "recalls" anyone. Mixing the two
    up here previously.
    """
    # Cast defensively: a numpy.int64 pulled straight out of a pandas column
    # (as callers do, via a selectbox built from results_for_comp_event's
    # dataframe) makes Mark.entry_id == entry_id silently match zero rows
    # instead of erroring -- caught by comparing against a hardcoded plain
    # int, which returned the expected 300 rows where the numpy value
    # returned none.
    entry_id = int(entry_id)
    comp_event_id = int(comp_event_id)

    rounds = list(session.scalars(select(Round).where(Round.comp_event_id == comp_event_id)).all())
    round_by_id = {r.id: r for r in rounds}
    if not round_by_id:
        return pd.DataFrame()

    stmt = (
        select(Mark, Person.display_name)
        .join(Person, Mark.judge_person_id == Person.id, isouter=True)
        .where(Mark.entry_id == entry_id, Mark.round_id.in_(list(round_by_id.keys())))
    )
    out = []
    for mark, judge_name in session.execute(stmt).all():
        round_row = round_by_id[mark.round_id]
        if mark.placement is not None:
            call, numeric, is_placement = f"Placed {mark.placement}", mark.placement, True
        elif mark.recalled is not None:
            call, numeric, is_placement = ("✓" if mark.recalled else "--"), int(mark.recalled), False
        else:
            call, numeric, is_placement = "unknown", None, False
        out.append(
            {
                "Round": "Final" if round_row.round_type == "final" else round_row.round_type,
                "Dance": mark.dance,
                "Judge": judge_name or "unknown",
                "Call": call,
                "_round_order": round_row.round_order,
                "_numeric": numeric,
                "_is_placement": is_placement,
            }
        )
    df = pd.DataFrame(out)
    if not df.empty:
        df["_dance_order"] = df["Dance"].map(lambda d: _dance_sort_key(d))
        df = df.sort_values(["_round_order", "_dance_order", "Judge"]).drop(columns="_dance_order").reset_index(drop=True)
    return df


def marks_detail_with_totals(df: pd.DataFrame) -> pd.DataFrame:
    """Inserts a "Total" row after each (round, dance) group, and a "Round
    total" row after each round's dances, into the raw marks detail table --
    the same arithmetic as marks_round_totals/marks_dance_totals, surfaced
    inline where every individual mark is listed. Labeled "sum" (not
    "placement") for the Final, since a plain sum isn't the actual Skating
    System result -- see marks_round_totals -- just the raw total of what's
    above it, kept separate to avoid re-implying sum decides placement."""
    if df.empty:
        return df
    # Cast _numeric to float64 up front: each synthetic "Total" row below is
    # a single-row block whose _numeric is entirely null, which pandas warns
    # about excluding from dtype inference on concat (FutureWarning) unless
    # every block already agrees on a dtype -- fixed here rather than
    # suppressed, since the fix is one line and _numeric is never read after
    # this function returns (hidden from display, not used downstream).
    df = df.astype({"_numeric": "float64"})
    blocks = []
    for (round_name, round_order), round_group in df.groupby(["Round", "_round_order"], sort=False):
        for dance, dance_group in round_group.groupby("Dance", sort=False):
            blocks.append(dance_group)
            is_placement = bool(dance_group["_is_placement"].iloc[0])
            total = int(dance_group["_numeric"].sum())
            call = f"Total: sum = {total}" if is_placement else f"Total: marked {total}/{len(dance_group)}"
            blocks.append(
                pd.DataFrame(
                    [
                        {
                            "Round": round_name,
                            "Dance": dance,
                            "Judge": "",
                            "Call": call,
                            "_round_order": round_order,
                            "_numeric": float("nan"),
                            "_is_placement": is_placement,
                        }
                    ]
                )
            )
        is_placement = bool(round_group["_is_placement"].iloc[0])
        total = int(round_group["_numeric"].sum())
        call = f"Round total: sum = {total}" if is_placement else f"Round total: marked {total}/{len(round_group)}"
        blocks.append(
            pd.DataFrame(
                [
                    {
                        "Round": round_name,
                        "Dance": "",
                        "Judge": "",
                        "Call": call,
                        "_round_order": round_order,
                        "_numeric": float("nan"),
                        "_is_placement": is_placement,
                    }
                ]
            )
        )
    return pd.concat(blocks, ignore_index=True)


def _skating_system_rank(votes_by_competitor: dict[int, list[int]]) -> dict[int, int]:
    """The Skating System / majority-rule algorithm real judged finals
    (NDCA, WDSF, figure skating) actually use to turn ordinal per-judge
    placements into an overall ranking -- NOT a sum or average, which is
    what this replaced (a user correctly flagged that "sum of placements"
    was misdescribing how a Final placement is really decided).

    To find 1st place: count how many votes rank each remaining competitor
    at or better than a rising threshold (1st, then 1st-or-2nd, then
    1st-2nd-or-3rd, ...) until someone reaches a majority (more than half
    of all votes cast). Whoever has the most votes at that threshold wins
    that place; ties are broken by summing that group's own placements
    (lower sum wins) since sum is a reasonable last resort, just not the
    primary method. Repeat among the remaining competitors for 2nd, 3rd,
    etc. This is a best-effort implementation of the standard method --
    it doesn't reproduce every organization-specific tie-break rule, so it
    may not always exactly match the official stored Result in edge cases.
    """
    total_voters = max((len(v) for v in votes_by_competitor.values()), default=0)
    if total_voters == 0:
        return {}
    majority = total_voters // 2 + 1
    remaining = set(votes_by_competitor.keys())
    result: dict[int, int] = {}
    place = 1
    while remaining:
        winners: list[int] = []
        threshold = 1
        while not winners and threshold <= total_voters:
            counts = {c: sum(1 for v in votes_by_competitor[c] if v <= threshold) for c in remaining}
            best = max(counts.values())
            if best >= majority:
                winners = [c for c, cnt in counts.items() if cnt == best]
            threshold += 1
        if not winners:
            # No majority ever reached (shouldn't happen with complete, full
            # rankings) -- fall back to lowest sum among what's left.
            winners = [min(remaining, key=lambda c: sum(votes_by_competitor[c]))]
        elif len(winners) > 1:
            winners.sort(key=lambda c: sum(votes_by_competitor[c]))
        for c in winners:
            result[c] = place
            place += 1
            remaining.discard(c)
    return result


def skating_system_results_for_final(session, comp_event_id: int) -> dict[str, dict[int, int]]:
    """Runs the Skating System over the Final round's raw judge marks for
    every couple in the field at once (majority rule is only meaningful
    relative to the whole field, not one couple in isolation).

    Returns per_dance: per_dance[dance][entry_id] is the couple's computed
    placement using only that one dance's judges' votes -- feeds "Totals by
    routine", where there's no official per-dance placement to fall back on
    (NDCA only stores the combined overall Final result). The overall
    combined placement itself is NOT recomputed here anymore: the official
    stored Result is authoritative and simpler to just display directly
    (see marks_round_totals) -- a prior version re-derived it via majority-
    of-dances for transparency, but the user decided the official number
    alone is enough for that row.
    """
    # Round.round_type is "final" (lowercase) for WDSF but "Final" (as-is
    # from source) for NDCA -- see the same normalization elsewhere in this
    # file ("Final" if round_type == "final" else round_type). A
    # case-sensitive match here silently found nothing for NDCA competitions.
    final_round = session.scalar(
        select(Round).where(Round.comp_event_id == comp_event_id, func.lower(Round.round_type) == "final")
    )
    if final_round is None:
        return {}
    marks = list(session.scalars(select(Mark).where(Mark.round_id == final_round.id, Mark.placement.is_not(None))).all())
    if not marks:
        return {}

    votes_by_dance: dict[str, dict[int, list[int]]] = {}
    for m in marks:
        votes_by_dance.setdefault(m.dance, {}).setdefault(m.entry_id, []).append(m.placement)

    return {dance: _skating_system_rank(votes) for dance, votes in votes_by_dance.items()}


def marks_round_totals(df: pd.DataFrame, official_placement: str | None = None) -> pd.DataFrame:
    """Per round: non-final rounds show how many judges marked this couple
    to advance (out of all judge-dance marks that round); the Final just
    states the official stored placement -- that's the authoritative
    number, so it's shown directly rather than re-derived (see
    skating_system_results_for_final's docstring for why a prior version
    computed its own placement here and why that was dropped).

    "Marked", not "recalled": a judge marks a couple in a non-final round;
    whether the couple is actually recalled is a round-level outcome decided
    once every judge's marks are tallied, not something any one judge does.
    """
    if df.empty:
        return df
    out = []
    for (round_name, round_order), group in df.groupby(["Round", "_round_order"]):
        if group["_is_placement"].any():
            total = f"Official: place {official_placement}" if official_placement is not None else "unknown"
        else:
            marked = int(group["_numeric"].sum())
            total = f"Marked by {marked}/{len(group)} judges"
        out.append({"Round": round_name, "Total": total, "_round_order": round_order})
    return pd.DataFrame(out).sort_values("_round_order").drop(columns="_round_order").reset_index(drop=True)


def marks_dance_totals(df: pd.DataFrame, per_dance_skating_placements: dict[str, int] | None = None) -> pd.DataFrame:
    """Per dance (routine), across the whole event: how many judge-marks
    this couple received in non-Final rounds, and the Skating System
    placement for that single dance if they made the Final."""
    if df.empty:
        return df
    per_dance_skating_placements = per_dance_skating_placements or {}
    out = []
    for dance in sorted(df["Dance"].unique(), key=_dance_sort_key):
        group = df[df["Dance"] == dance]
        marks_given = group[~group["_is_placement"]]
        marks_str = f"{int(marks_given['_numeric'].sum())}/{len(marks_given)}" if not marks_given.empty else "--"
        placement = per_dance_skating_placements.get(dance)
        final_str = f"place {placement}" if placement is not None else "--"
        out.append({"Dance": dance, "Judges' marks": marks_str, "Final placement (this dance)": final_str})
    return pd.DataFrame(out).reset_index(drop=True)


def marks_judge_totals(df: pd.DataFrame) -> pd.DataFrame:
    """Per judge, across the whole event: how often they marked this couple
    to advance, and the sum of placements they personally gave in the
    Final -- surfaces whether a particular judge was consistently harsher
    or kinder."""
    if df.empty:
        return df
    out = []
    for judge, group in df.groupby("Judge"):
        marks_given = group[~group["_is_placement"]]
        final_votes = group[group["_is_placement"]]
        marks_str = f"{int(marks_given['_numeric'].sum())}/{len(marks_given)}" if not marks_given.empty else "--"
        final_str = str(int(final_votes["_numeric"].sum())) if not final_votes.empty else "--"
        out.append({"Judge": judge, "Marks given": marks_str, "Final placement sum given": final_str})
    return pd.DataFrame(out).reset_index(drop=True)


def _marks_raw_for_event(session, comp_event_id: int) -> pd.DataFrame:
    """One row per (entry, round, dance) total judges' marks for the whole
    event -- the shared raw material for marks_tabulation_for_event and
    marks_by_routine_for_event below, so a field-wide "every couple side
    by side" view stays in agreement with marks_detail_for_entry's own
    per-couple totals instead of re-deriving the same arithmetic twice.
    Sums straight over judges here (marks_detail_for_entry keeps the
    per-judge rows since its callers break totals down by judge too;
    these two callers only ever want the round/dance total).

    The Final round's judges give a per-dance placement (Mark.placement),
    not a recall mark -- its "total" is the sum of those placements
    (lower is better, unlike every other round's mark count), the same
    unofficial "sum, not the real Skating System result" arithmetic
    marks_detail_with_totals already surfaces per couple; the official
    result is Placement, already its own column on the main results
    table."""
    rounds = list(session.scalars(select(Round).where(Round.comp_event_id == comp_event_id)).all())
    if not rounds:
        return pd.DataFrame()
    round_by_id = {r.id: r for r in rounds}
    rows = session.execute(
        select(Mark.entry_id, Mark.round_id, Mark.dance, Mark.recalled, Mark.placement).where(
            Mark.round_id.in_(list(round_by_id.keys()))
        )
    ).all()
    out = []
    for entry_id, round_id, dance, recalled, placement in rows:
        round_row = round_by_id[round_id]
        numeric = placement if placement is not None else (int(recalled) if recalled is not None else 0)
        out.append(
            {
                "_entry_id": entry_id,
                "_round_order": round_row.round_order,
                "Round": "Final" if round_row.round_type.lower() == "final" else round_row.round_type,
                "Dance": dance,
                "_numeric": numeric,
            }
        )
    return pd.DataFrame(out)


def marks_tabulation_for_event(session, comp_event_id: int) -> pd.DataFrame:
    """Every couple's total marks per round, side by side, in one table --
    a field-wide companion to marks_round_totals, which only covers
    whichever one couple is picked from the dropdown below. Non-Final
    columns: higher is better (how many of that round's judge-dance marks
    went this couple's way). Final: lower is better (see
    _marks_raw_for_event for why it's a raw placement sum, not the
    official result)."""
    raw = _marks_raw_for_event(session, comp_event_id)
    if raw.empty:
        return raw
    round_order = raw[["_round_order", "Round"]].drop_duplicates().sort_values("_round_order")["Round"].tolist()
    totals = raw.groupby(["_entry_id", "Round"])["_numeric"].sum().reset_index()
    pivot = totals.pivot(index="_entry_id", columns="Round", values="_numeric")[round_order]
    return pivot.reset_index()


def marks_by_routine_for_event(session, comp_event_id: int) -> pd.DataFrame:
    """Every couple's marks broken down by round AND dance, side by side --
    the routine-level companion to marks_tabulation_for_event (which only
    totals a whole round), for seeing which specific dance cost a couple
    marks rather than just the round total. Dances ordered via
    _dance_sort_key within each round, the same syllabus order
    marks_dance_totals and marks_detail_for_entry already use."""
    raw = _marks_raw_for_event(session, comp_event_id)
    if raw.empty:
        return raw
    combos = raw[["_round_order", "Round", "Dance"]].drop_duplicates().copy()
    combos["_dance_order"] = combos["Dance"].map(_dance_sort_key)
    combos["_col"] = combos["Round"] + " - " + combos["Dance"]
    column_order = combos.sort_values(["_round_order", "_dance_order"])["_col"].tolist()

    raw = raw.copy()
    raw["_col"] = raw["Round"] + " - " + raw["Dance"]
    totals = raw.groupby(["_entry_id", "_col"])["_numeric"].sum().reset_index()
    pivot = totals.pivot(index="_entry_id", columns="_col", values="_numeric")[column_order]
    return pivot.reset_index()


def _labeled_marks_pivot(results_df: pd.DataFrame, pivot: pd.DataFrame) -> pd.DataFrame:
    """Left-joins a marks_tabulation_for_event/marks_by_routine_for_event
    pivot onto the main results table's Couple/Placement columns, in that
    table's own final-result order -- a couple knocked out before some
    later round/dance just gets a blank cell there rather than a row of
    its own -- Int64 (pandas' nullable integer type), not the plain
    float64-with-NaN pivot() itself produces: both display a blank cell
    for the missing entries, but a plain float column's whole numbers
    round-trip through CSV as "13.0" instead of "13"."""
    if pivot.empty:
        return pivot
    combined = results_df[["Couple", "Placement", "_entry_id"]].merge(pivot, on="_entry_id", how="left")
    combined = combined.drop(columns="_entry_id")
    for col in combined.columns:
        if col not in ("Couple", "Placement"):
            combined[col] = combined[col].astype("Int64")
    return combined


def competition_search(session, *, linked_competition_id: int | None = None, linked_event_id: int | None = None) -> None:
    """linked_competition_id/linked_event_id come from the "View" link
    column on the dancer page (see main()) -- when set and the search box
    is still empty (a fresh arrival via that link, not a manual search),
    they skip straight to that competition/event instead of requiring the
    name search below. A non-empty term always takes over from a stale
    link still sitting in the URL, dropping linked_event_id too (it likely
    belongs to a different competition than whatever the new search
    finds)."""
    term = st.text_input("Search for a competition by name", "", key="competition_search_term")
    if not term:
        if linked_competition_id is None:
            st.info("Type a competition name above (e.g. 'Emerald Ball', 'U.S. National').")
            return
        competition = session.get(Competition, linked_competition_id)
        if competition is None:
            st.warning(f"No competition found with id={linked_competition_id}.")
            return
    else:
        linked_event_id = None
        competitions = search_competitions(session, term)
        if not competitions:
            st.warning(f"No competitions found matching {term!r}.")
            return

        if len(competitions) == 1:
            competition = competitions[0]
        else:
            options = {f"{c.name} ({c.start_date}) -- id {c.id}": c for c in competitions}
            choice = st.selectbox("Multiple matches -- pick one:", list(options.keys()))
            competition = options[choice]

    date_range = _format_date_range(competition.start_date, competition.end_date)
    subtitle = ", ".join(p for p in (date_range, competition.city) if p)
    # A plain st.header + st.caption pair (tried first) put the subtitle
    # both too small (caption's muted small text) and too far below the
    # title (each is its own block with its own margin, and a 36px
    # heading's own line-height added more gap on top of that) -- per
    # user feedback. A negative margin on the subtitle's own div is the
    # only way to pull it up against the heading's line-height without
    # shrinking the heading itself, which plain markdown block-grouping
    # can't do -- the one unsafe_allow_html in this file, so the
    # interpolated name/subtitle (scraped from NDCA, not user input, but
    # not implicitly trusted either) are HTML-escaped rather than passed
    # through raw.
    st.markdown(f"## {escape(competition.name)}", unsafe_allow_html=False)
    if subtitle:
        st.markdown(
            f"<div style='margin-top:-0.75rem;font-size:1.05rem'>{escape(subtitle)}</div>",
            unsafe_allow_html=True,
        )

    events = events_for_competition(session, competition.id)
    if not events:
        st.warning("No events on file for this competition yet.")
        return

    # Counts come from one aggregate query, not one per event -- a large
    # competition (e.g. Austin Star Ball has ~500 events) made per-event
    # queries plus rendering every event's full table at once take a
    # minute-plus and was heavy enough to crash the app. Only the single
    # event the user picks below gets its full result table computed.
    counts = event_result_counts(session, competition.id)
    total_entries = sum(counts.values())
    st.subheader(f"Events ({len(events)}) -- {total_entries} entries total")

    styles = sorted({e.style or "unknown" for e in events})
    filter_cols = st.columns(2)
    style_choice = filter_cols[0].selectbox("Filter by style", ["All"] + styles)
    title_filter = filter_cols[1].text_input("Filter by event title (e.g. 'Youth', 'Championship')", "")
    filtered = [e for e in events if style_choice == "All" or (e.style or "unknown") == style_choice]
    if title_filter:
        filtered = [e for e in filtered if title_filter.lower() in e.raw_title.lower()]
    filtered.sort(key=lambda e: counts.get(e.id, 0), reverse=True)

    if not filtered:
        st.write("No events match this filter.")
        return

    event_options = {f"{e.raw_title} -- {counts.get(e.id, 0)} entries": e for e in filtered}
    option_labels = list(event_options.keys())
    default_index = 0
    if linked_event_id is not None:
        for i, e in enumerate(event_options.values()):
            if e.id == linked_event_id:
                default_index = i
                break
    event_choice = st.selectbox("Select an event to view results", option_labels, index=default_index)
    event = event_options[event_choice]

    df = results_for_comp_event(session, event.id)
    if df.empty:
        st.write("No results on file for this event.")
    else:
        display_cols = [c for c in df.columns if not c.startswith("_")]

        # Computed here, right after the event is picked, rather than
        # inside the "Marks tabulation" expander below where they're also
        # used -- the summary report button (right below the event
        # selection dropdown, per user request) needs them too, and
        # computing them twice would double the query cost for no reason.
        tabulation = _labeled_marks_pivot(df, marks_tabulation_for_event(session, event.id))
        routine = _labeled_marks_pivot(df, marks_by_routine_for_event(session, event.id))

        st.download_button(
            "Generate event summary report (PDF)",
            _event_summary_pdf_bytes(competition, event, df, display_cols, tabulation, routine),
            file_name=f"{competition.name} - {event.raw_title} summary.pdf".replace("/", "-"),
            mime="application/pdf",
            key=f"event_summary_{event.id}",
        )

        st.dataframe(
            df,
            use_container_width=True,
            hide_index=True,
            column_order=display_cols,
            column_config=_PARTNER_LINK_COLUMN_CONFIG,
        )
        _download_buttons(df, display_cols, f"{competition.name} - {event.raw_title} results", event.raw_title)

        with st.expander("Marks tabulation (every couple, by round and routine)"):
            st.caption(
                "Every couple, in final result order. Non-Final columns: total judges' "
                "marks that round/dance (higher is better). Final column(s): sum of "
                "judges' per-dance placements (lower is better) -- see Placement above "
                "for the official result."
            )
            if tabulation.empty:
                st.write("No marks on file for this event.")
            else:
                st.write("**Total marks by round**")
                st.dataframe(tabulation, use_container_width=True, hide_index=True)
                _download_buttons(
                    tabulation,
                    list(tabulation.columns),
                    f"{competition.name} - {event.raw_title} marks tabulation",
                    f"{event.raw_title} -- Marks Tabulation",
                )
                # Final excluded from the chart (though not the table above)
                # -- its placement-sum is a different scale than every other
                # round's mark count (100s vs 10s-60s) and runs the opposite
                # direction (lower is better), so a bar chart mixing them in
                # dwarfed the marks bars and made a worse Final look like a
                # bigger, "better" bar. Per user feedback.
                round_cols = [c for c in tabulation.columns if c not in ("Couple", "Placement", "Final")]
                _grouped_bar_chart(tabulation.set_index("Couple")[round_cols], round_cols)

                st.write("**Marks by routine and round**")
                st.dataframe(routine, use_container_width=True, hide_index=True)
                _download_buttons(
                    routine,
                    list(routine.columns),
                    f"{competition.name} - {event.raw_title} marks by routine",
                    f"{event.raw_title} -- Marks by Routine",
                )
                # One grouped bar chart per round (not all round-dance columns
                # at once -- e.g. 4 rounds x 5 dances is unreadable as a single
                # chart), picked via a dropdown rather than a chart per round
                # so the page doesn't grow unbounded with more rounds.
                routine_cols = [c for c in routine.columns if c not in ("Couple", "Placement")]
                if routine_cols:
                    rounds_present = list(dict.fromkeys(c.split(" - ", 1)[0] for c in routine_cols))
                    chart_round = st.selectbox(
                        "Chart which round?", rounds_present, key=f"routine_chart_round_{event.id}"
                    )
                    chart_cols = [c for c in routine_cols if c.split(" - ", 1)[0] == chart_round]
                    _grouped_bar_chart(routine.set_index("Couple")[chart_cols], chart_cols)

        if not tabulation.empty:
            with st.expander("Compare couples head-to-head"):
                # Anchor picked first (pill buttons, single-select -- one
                # couple, e.g. "our couple") then who to compare them
                # against (pill buttons, multi-select -- a couple of
                # rivals), rather than one flat multiselect for all of them
                # -- per user feedback, picking an anchor first reads more
                # naturally than hunting for 2-3 names in one list with no
                # sense of which one you actually care about, and the same
                # pill-button style reads better than a dropdown for the
                # anchor too. The "compare against" pills still only appear
                # once an anchor is picked -- with no anchor there's nothing
                # yet to compare against.
                couple_choices = df["Couple"].tolist()
                anchor = st.pills(
                    "Anchor couple",
                    couple_choices,
                    selection_mode="single",
                    key=f"compare_anchor_{event.id}",
                )
                compare_choices = []
                if anchor is None:
                    st.info("Pick an anchor couple to compare.")
                else:
                    others = st.pills(
                        "Compare against",
                        [c for c in couple_choices if c != anchor],
                        selection_mode="multi",
                        key=f"compare_others_{event.id}",
                    )
                    if len(others) > 2:
                        st.warning("Only the first 2 picks below are used.")
                        others = others[:2]
                    compare_choices = [anchor] + others
                if not compare_choices or len(compare_choices) < 2:
                    if anchor is not None:
                        st.info("Pick 1 or 2 couples to compare against the anchor.")
                else:
                    # Transposed (couples as columns, metrics as rows) rather
                    # than the field-wide tables' own shape -- side by side is
                    # the whole point of a head-to-head view, and reads far
                    # better for 2-3 couples than 2-3 rows in a wide table.
                    # Stringified via _pdf_cell_str (blank for a round/dance a
                    # couple never reached, plain int otherwise) before the
                    # transpose -- an Int64 column's pd.NA survives a
                    # transpose as a raw Python object, which broke Arrow
                    # serialization the same way plain int/"" mixing did
                    # earlier (see _labeled_marks_pivot).
                    compare_tab = tabulation.set_index("Couple").loc[compare_choices]
                    st.write("**Total marks by round**")
                    st.dataframe(compare_tab.map(_pdf_cell_str).T, use_container_width=True)
                    # Final excluded from the chart for the same reason as the
                    # field-wide chart above: different scale, opposite
                    # direction (lower is better) from every other round.
                    compare_round_cols = [c for c in compare_tab.columns if c not in ("Placement", "Final")]
                    _grouped_bar_chart(compare_tab[compare_round_cols], compare_round_cols)

                    compare_routine = routine.set_index("Couple").loc[compare_choices]
                    st.write("**Marks by routine and round**")
                    st.dataframe(compare_routine.map(_pdf_cell_str).T, use_container_width=True)
                    compare_routine_cols = [c for c in compare_routine.columns if c != "Placement"]
                    if compare_routine_cols:
                        compare_rounds_present = list(
                            dict.fromkeys(c.split(" - ", 1)[0] for c in compare_routine_cols)
                        )
                        compare_chart_round = st.selectbox(
                            "Chart which round?", compare_rounds_present, key=f"compare_chart_round_{event.id}"
                        )
                        compare_chart_cols = [
                            c for c in compare_routine_cols if c.split(" - ", 1)[0] == compare_chart_round
                        ]
                        _grouped_bar_chart(compare_routine[compare_chart_cols], compare_chart_cols)

        st.subheader("Judges' marks")
        couple_options = dict(zip(df["Couple"], df["_entry_id"]))
        couple_choice = st.selectbox("Select a couple to see judges' marks", list(couple_options.keys()))
        entry_id = int(couple_options[couple_choice])  # numpy.int64 from the dataframe -- see marks_detail_for_entry
        official_placement = df.loc[df["Couple"] == couple_choice, "Placement"].iloc[0]

        marks_df = marks_detail_for_entry(session, entry_id, event.id)
        if marks_df.empty:
            st.write("No judges' marks on file for this couple in this event.")
        else:
            per_dance_skating = skating_system_results_for_final(session, event.id)
            round_totals = marks_round_totals(marks_df, official_placement)
            dance_totals = marks_dance_totals(marks_df, {d: p.get(entry_id) for d, p in per_dance_skating.items()})
            judge_totals = marks_judge_totals(marks_df)

            mcols = st.columns(3)
            mcols[0].write("**Totals by round**")
            mcols[0].dataframe(round_totals, use_container_width=True, hide_index=True)
            mcols[1].write("**Totals by routine**")
            mcols[1].dataframe(dance_totals, use_container_width=True, hide_index=True)
            mcols[2].write("**Totals by judge**")
            mcols[2].dataframe(judge_totals, use_container_width=True, hide_index=True)

            with st.expander("Full marks detail (every judge, every dance, every round)"):
                detail_df = marks_detail_with_totals(marks_df)
                detail_cols = [c for c in detail_df.columns if not c.startswith("_")]
                st.dataframe(detail_df, use_container_width=True, hide_index=True, column_order=detail_cols)
                _download_buttons(
                    detail_df,
                    detail_cols,
                    f"{competition.name} - {event.raw_title} - {couple_choice} marks",
                    f"{event.raw_title} -- {couple_choice}",
                )


def heat_list_search(session, *, linked_competition_id: int | None = None, linked_event_id: str | None = None) -> None:
    """Pre-competition schedules -- NDCA Premier only for now (see
    dsr.models.ScheduledHeat), loaded via scripts/bulk_load_ndca_heatlists.py.
    A pulldown of every competition with a published heat list, not a
    name search first like competition_search -- there are only ever a
    handful on file at any given time, so listing them directly (and
    letting the user just pick one) is more useful than making them
    guess a name to search.

    linked_competition_id/linked_event_id come from a "View event" link
    on this same page (see heat_list_for_competition and main()) -- when
    set, the pulldown defaults to that competition. If the user picks a
    different one instead, linked_event_id is dropped -- it belonged to
    whichever competition the link named, not the newly-picked one."""
    db_identity = _db_identity(session)
    st.caption("Pre-competition schedules for upcoming/in-progress NDCA Premier competitions.")
    competitions = competitions_with_heatlists(session)
    if not competitions:
        st.info("No competitions with a published heat list on file right now.")
        return

    options = {f"{c.name} ({c.start_date})": c for c in competitions}
    option_labels = list(options.keys())
    default_index = 0
    if linked_competition_id is not None:
        for i, c in enumerate(competitions):
            if c.id == linked_competition_id:
                default_index = i
                break
    choice = st.selectbox("Select a competition", option_labels, index=default_index)
    competition = options[choice]
    if competition.id != linked_competition_id:
        linked_event_id = None

    st.header(competition.name)
    cols = st.columns(3)
    cols[0].metric("Dates", _format_date_range(competition.start_date, competition.end_date))
    cols[1].metric("Location", ", ".join(p for p in (competition.city, competition.country) if p) or "unknown")
    cols[2].metric("Sanctioning body", competition.sanctioning_body or "unknown")

    df = heat_list_for_competition(session, db_identity, competition.id)
    if df.empty:
        st.write("No scheduled heats on file for this competition.")
        return

    if linked_event_id is not None:
        # Real confusion this caused: the filters below stayed visible on
        # the single-event view, but a name/style/etc. search only ever
        # narrowed *within* that one locked event, with no visible way to
        # widen back out -- looked like the filters were broken rather
        # than just scoped tighter than they appeared. This link drops
        # just the event lock (via st.query_params, so it survives the
        # rerun) and falls back to browsing this competition's full
        # schedule, same as picking it from the dropdown fresh.
        if st.button("Show full competition schedule"):
            st.query_params.pop("heat_event_id", None)
            st.rerun()

    # Three pulldowns (style/category/floor) stacked in one column, under
    # the text-filter row's third slot -- keeps the two free-text filters
    # (name, event) and the first pulldown on one clean line, per user
    # request, rather than 5 filters crowded into one row. The event
    # filter's label is kept short (example moved to `help`, a hover
    # tooltip) specifically so it stays one line -- a wrapped 2-line
    # label pushed its own input box down out of line with its neighbors.
    filter_cols = st.columns(3)
    name_filter = filter_cols[0].text_input("Filter by dancer name", "")
    event_filter = filter_cols[1].text_input("Filter by event title", "", help="e.g. 'Youth', 'Under 21'")
    styles = sorted(s for s in df["Style"].unique().tolist())
    style_choice = filter_cols[2].selectbox("Filter by style", ["All"] + styles)
    categories = sorted(c for c in df["Category"].unique().tolist())
    category_choice = filter_cols[2].selectbox("Filter by category", ["All"] + categories)
    floors = sorted(f for f in df["Floor"].dropna().unique().tolist())
    floor_choice = filter_cols[2].selectbox("Filter by floor", ["All"] + floors)
    number_cols = st.columns(2)
    heat_number_filter = number_cols[0].text_input("Filter by heat number", "", help="Exact match, e.g. '12'")
    couple_number_filter = number_cols[1].text_input("Filter by couple number", "", help="Exact match, e.g. '507'")
    hide_danced = st.checkbox("Hide heats already danced", value=True)

    filtered = df
    if name_filter:
        filtered = filtered[filtered["Couple"].str.contains(name_filter, case=False, na=False)]
    if event_filter:
        filtered = filtered[filtered["Event"].str.contains(event_filter, case=False, na=False)]
    elif linked_event_id is not None and not name_filter:
        # Exact match on the stable source event id, not the title text --
        # a substring filter could also catch unrelated events that
        # happen to share wording (e.g. the same division name at a
        # different level). Only applied when the user hasn't typed a
        # name filter of their own -- typing a name is a clear signal
        # they want to search this competition's *whole* schedule for
        # that dancer, not stay boxed into the one event they linked in
        # from (see the "Show full competition schedule" button above).
        filtered = filtered[filtered["_source_event_id"] == linked_event_id]
    if style_choice != "All":
        filtered = filtered[filtered["Style"] == style_choice]
    if category_choice != "All":
        filtered = filtered[filtered["Category"] == category_choice]
    if floor_choice != "All":
        filtered = filtered[filtered["Floor"] == floor_choice]
    if heat_number_filter.strip():
        filtered = filtered[filtered["Heat #"].astype(str) == heat_number_filter.strip()]
    if couple_number_filter.strip():
        filtered = filtered[filtered["Start #"].astype(str) == couple_number_filter.strip()]
    if hide_danced:
        filtered = filtered[filtered["Danced"] == "No"]

    st.subheader(f"{len(filtered)} of {len(df)} scheduled heats")
    if filtered.empty:
        st.write("No scheduled heats match this filter.")
        return

    display_cols = [c for c in df.columns if not c.startswith("_")]

    # Whole-filtered-set download, ahead of the render branches below --
    # the grouped-by-partnership view (see below) otherwise only offers
    # Streamlit's own per-table "Download as CSV" toolbar button one
    # expander at a time, with no way to get the whole filtered schedule
    # in one file.
    _download_buttons(filtered, display_cols, f"{competition.name} heat list", f"{competition.name} -- Heat List")

    # A single event (every remaining row shares one source_event_id --
    # true right after a "View event" link, and also whenever a manual
    # event-title filter happens to narrow to just one division) renders
    # as one flat table of every couple in that field, same shape as the
    # Competition page's own results table -- not grouped by partnership,
    # since there's exactly one heat per couple here and grouping would
    # just wrap each single row in its own pointless expander. The
    # self-referential "View event" link is dropped too, for the same
    # reason the Competition page's results table has no such column.
    if filtered["_source_event_id"].nunique() == 1:
        st.write(f"**{filtered['Event'].iloc[0]}**")
        # "Event" dropped too -- it's the same value on every row once
        # the table is already scoped to this one event, same reasoning
        # as dropping the self-referential "View event" link.
        event_cols = [c for c in display_cols if c not in ("View event", "Event")]
        st.dataframe(
            filtered, use_container_width=True, hide_index=True, column_order=event_cols,
            column_config=_HEAT_LIST_LINK_COLUMN_CONFIG,
        )
        return

    # Otherwise, grouped by partnership (one expander per couple, each
    # showing just their own heats sorted by time) rather than one giant
    # flat table -- real feedback: a dancer with several partnerships
    # (e.g. a Pro-Am instructor) had every partner's heats interleaved in
    # one table with no way to tell them apart. Capped rather than always
    # grouping: with no name/event narrowing, a competition-wide view can
    # be 900+ partnerships, and rendering that many expanders at once is
    # neither fast nor useful -- the flat table (sorted by time, which is
    # what a "what's coming up next" view actually wants) stays the
    # fallback.
    partnership_ids = filtered["_partnership_id"].unique()
    if len(partnership_ids) > 40:
        st.caption(f"{len(partnership_ids)} different partnerships match -- narrow by dancer name to group by couple.")
        st.dataframe(
            filtered, use_container_width=True, hide_index=True, column_order=display_cols,
            column_config=_HEAT_LIST_LINK_COLUMN_CONFIG,
        )
        return

    groups = list(filtered.groupby("_partnership_id", sort=False))
    groups.sort(
        key=lambda pair: pair[1]["_scheduled_time"].iloc[0]
        if not pair[1]["_scheduled_time"].isna().all()
        else pd.Timestamp.max
    )
    for _partnership_id, group in groups:
        couple = group["Couple"].iloc[0]
        with st.expander(f"{couple} -- {len(group)} heats", expanded=False):
            st.dataframe(
                group, use_container_width=True, hide_index=True, column_order=display_cols,
                column_config=_HEAT_LIST_LINK_COLUMN_CONFIG,
            )


def dancer_search(
    session, *, linked_person_id: int | None = None, linked_partnership_id: int | None = None
) -> None:
    """linked_person_id comes from a "Partner" link column on the
    Competition page or Heat Lists (see main(), _field_results_for_comp_events,
    and heat_list_for_competition) -- when set and the search box is still
    empty (a fresh arrival via that link, not a manual search), it skips
    straight to that person instead of requiring a name search. Same
    "manual search always wins" rule as competition_search's
    linked_competition_id/linked_event_id.

    linked_partnership_id (same link, carried alongside linked_person_id)
    narrows the page further to just that one partnership instead of
    every partnership this person has ever had -- real request: clicking
    through from a specific couple's heat wants that couple's own
    history, not a search through everyone this person has ever danced
    with to find them again. Same "manual search always wins" rule, plus
    its own "show every partnership" button to drop just this narrower
    lock (see the "Show full competition schedule" button on Heat Lists
    for the same pattern)."""
    db_identity = _db_identity(session)
    term = st.text_input("Search for a dancer by name", "", key="dancer_search_term")
    if not term:
        if linked_person_id is None:
            st.info("Type a name above to search (matches display name and any known alias spelling).")
            return
        person = session.get(Person, linked_person_id)
        if person is None:
            st.warning(f"No dancer found with id={linked_person_id}.")
            return
        people = [person]
    else:
        people = search_people(session, db_identity, term)
        if not people:
            st.warning(f"No one found matching {term!r}.")
            return

    if len(people) == 1:
        person = people[0]
    else:
        options = {f"{p.display_name} ({p.country or 'unknown country'})  -- id {p.id}": p for p in people}
        choice = st.selectbox("Multiple matches -- pick one:", list(options.keys()))
        person = options[choice]

    partnerships = partnerships_for_person(session, db_identity, person.id)
    if not partnerships:
        st.header(person.display_name)
        st.warning("No partnerships/entries on file for this person yet.")
        return

    # Narrow to just the linked partnership on a fresh arrival via link
    # (manual search box use, or a prior "Show every partnership" click,
    # always wins -- same rule as linked_person_id above). Falls back to
    # showing every partnership if the id doesn't match any of this
    # person's own (stale link, e.g. a merged/renamed partnership) rather
    # than silently showing nothing.
    narrowed_to_one = False
    if not term and linked_partnership_id is not None:
        narrowed = [p for p in partnerships if p.id == linked_partnership_id]
        if narrowed:
            partnerships = narrowed
            narrowed_to_one = True
            if st.button("Show every partnership"):
                st.query_params.pop("partnership_id", None)
                st.rerun()

    # Both names when narrowed to one partnership -- real feedback: the
    # header showed only the person clicked through from, not the couple
    # this narrowed view is actually about, on a page whose whole point is
    # one specific partnership rather than one specific person.
    st.header(partner_label(session, partnerships[0]) if narrowed_to_one else person.display_name)
    # Country is only ever known for WDSF-sourced people (NDCA doesn't
    # capture it) -- shown when we actually have it, hidden rather than a
    # placeholder "unknown" otherwise, per user request.
    metrics = [("Role", "Adjudicator" if person.is_adjudicator else "Competitor")]
    if person.country:
        metrics.insert(0, ("Country", person.country))
    cols = st.columns(len(metrics))
    for col, (label, value) in zip(cols, metrics):
        col.metric(label, value)

    # Each partnership's results are pulled in one batched query
    # (result_histories_for_partnerships), not one query per partnership
    # -- a dancer with dozens of partnerships previously issued dozens of
    # sequential round trips per page load, invisible locally (SQLite has
    # ~0 round-trip cost) but ~20s against a real network database (Neon,
    # ~85ms/round-trip) before this batching.
    partnership_ids = [p.id for p in partnerships]
    histories_by_id = result_histories_for_partnerships(session, db_identity, partnership_ids)

    total_results = sum(len(df) for df in histories_by_id.values())
    st.subheader(f"Partnerships ({len(partnerships)}) -- {total_results} results total")

    # Group by category (students vs. amateur-couple partners vs.
    # professional partners) when we have the signal for it -- an
    # instructor with dozens of Pro-Am students otherwise mixes them into
    # one flat list with their own competitive partnerships. Split per
    # result rather than per partnership (see _split_history_by_category):
    # a partnership that changed category over time appears once per
    # section it actually has results in, each showing only that
    # section's own results. Only shown as separate sections when more
    # than one category is actually present.
    categorized: dict[str, list] = {}
    for partnership in partnerships:
        for category, sub_df in _split_history_by_category(
            histories_by_id[partnership.id], partnership_id=partnership.id
        ).items():
            categorized.setdefault(category, []).append((partnership, sub_df))

    category_order = [
        "Competitive partners",
        "Instructor-style",
        "Other partnerships",
    ]
    show_headers = sum(1 for c in category_order if categorized.get(c)) > 1

    for category in category_order:
        group = categorized.get(category)
        if not group:
            continue
        # Most-recent-result-first within each section -- each df is
        # already ordered by Competition.start_date descending (inherited
        # from the split), so its first row is that entry's latest
        # result; empty-result partnerships (no Date to sort by) sort
        # last.
        group.sort(key=lambda pair: pair[1]["Date"].iloc[0] if not pair[1].empty else dt.date.min, reverse=True)
        if show_headers:
            st.markdown(f"**{category}** ({len(group)})")
        for partnership, df in group:
            label = partner_label(session, partnership)
            with st.expander(f"{label} -- {len(df)} results", expanded=False):
                if df.empty:
                    st.write("No results on file for this partnership.")
                    continue

                st.dataframe(df, use_container_width=True, hide_index=True, column_config=_VIEW_LINK_COLUMN_CONFIG)
                _download_buttons(df, list(df.columns), f"{label} results", label)

                best = best_results(df)
                if not best.empty:
                    st.write("**Best results**")
                    st.dataframe(
                        best, use_container_width=True, hide_index=True, column_config=_VIEW_LINK_COLUMN_CONFIG
                    )


def main() -> None:
    session = _session()
    st.title("DanceSport Wiki")

    # A "View"/"Partner"/"View event" link clicked on any page
    # (opens a new tab, so this is a fresh session -- see
    # result_histories_for_partnerships, _field_results_for_comp_events,
    # and heat_list_for_competition) lands here with one of these pairs
    # set; used to jump straight to that exact event/dancer/heat-list
    # instead of making the user re-search for it.
    linked_competition_id = st.query_params.get("competition_id")
    linked_event_id = st.query_params.get("event_id")
    linked_person_id = st.query_params.get("person_id")
    linked_partnership_id = st.query_params.get("partnership_id")
    linked_heat_competition_id = st.query_params.get("heat_competition_id")
    linked_heat_event_id = st.query_params.get("heat_event_id")

    # A link opens in a new tab (see the comment above), so there's no
    # in-app history to go "back" through -- without this, the only way
    # off a linked-in view was to close the tab or hand-edit the URL.
    # Clearing every query param drops all three linked_* pairs at once
    # and re-picks the default (unlinked) Dancer-search view below.
    if st.query_params:
        if st.button("← New search"):
            st.query_params.clear()
            st.rerun()

    mode_options = ["Dancer", "Competition", "Heat Lists"]
    if linked_heat_competition_id:
        default_mode_index = mode_options.index("Heat Lists")
    elif linked_competition_id:
        default_mode_index = mode_options.index("Competition")
    else:
        default_mode_index = 0
    mode = st.radio("Search by", mode_options, horizontal=True, index=default_mode_index)
    if mode == "Dancer":
        dancer_search(
            session,
            linked_person_id=int(linked_person_id) if linked_person_id else None,
            linked_partnership_id=int(linked_partnership_id) if linked_partnership_id else None,
        )
    elif mode == "Competition":
        competition_search(
            session,
            linked_competition_id=int(linked_competition_id) if linked_competition_id else None,
            linked_event_id=int(linked_event_id) if linked_event_id else None,
        )
    else:
        heat_list_search(
            session,
            linked_competition_id=int(linked_heat_competition_id) if linked_heat_competition_id else None,
            linked_event_id=linked_heat_event_id,
        )


if __name__ == "__main__":
    main()
