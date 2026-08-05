"""M2 success check (spec): "search a name, get a correct multi-event history."

Run with: streamlit run app.py
"""
from __future__ import annotations

import pandas as pd
import streamlit as st
from sqlalchemy import func, or_, select

from dsr.db import get_engine, get_session
from dsr.models import CompEvent, Competition, Entry, Mark, Partnership, Person, PersonAlias, Result, Round

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


def search_people(session, term: str, limit: int = 25) -> list[Person]:
    term_like = f"%{term}%"
    alias_person_ids = select(PersonAlias.person_id).where(PersonAlias.raw_name.ilike(term_like))
    stmt = (
        select(Person)
        .where(or_(Person.display_name.ilike(term_like), Person.id.in_(alias_person_ids)))
        .order_by(Person.display_name)
        .limit(limit)
    )
    return list(session.scalars(stmt).all())


def partnerships_for_person(session, person_id: int) -> list[Partnership]:
    stmt = select(Partnership).where(
        or_(
            Partnership.leader_id == person_id,
            Partnership.follower_id == person_id,
            Partnership.student_id == person_id,
        )
    )
    return list(session.scalars(stmt).all())


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


def result_history_for_partnership(session, partnership_id: int) -> pd.DataFrame:
    stmt = (
        select(Competition, CompEvent, Entry, Result)
        .select_from(Result)
        .join(Entry, Result.entry_id == Entry.id)
        .join(CompEvent, Result.comp_event_id == CompEvent.id)
        .join(Competition, CompEvent.competition_id == Competition.id)
        .where(Entry.partnership_id == partnership_id)
        .order_by(Competition.start_date.desc())
    )
    result_rows = session.execute(stmt).all()
    highest_round_by_key = _highest_round_labels_for_entries(
        session, [(entry.id, comp_event.id) for _, comp_event, entry, _ in result_rows]
    )

    # Not-recalled rows reuse results_for_comp_event's own Placement string
    # (e.g. "9th (10 marks)") rather than recomputing the same field-wide
    # ranking here -- that keeps the dancer view and the competition view
    # permanently in agreement instead of drifting out of sync, and this was
    # the bug: the dancer view still showed a bare "not recalled" after the
    # competition view was changed to show rank + marks. One
    # results_for_comp_event call per not-recalled event (cached here so a
    # repeat event isn't recomputed), not per row.
    event_results_cache: dict[int, pd.DataFrame] = {}

    rows = []
    for competition, comp_event, entry, result in result_rows:
        if result.placement_low is None:
            if comp_event.id not in event_results_cache:
                event_results_cache[comp_event.id] = results_for_comp_event(session, comp_event.id)
            event_df = event_results_cache[comp_event.id]
            match = event_df.loc[event_df["_entry_id"] == entry.id, "Placement"]
            placement = match.iloc[0] if not match.empty else "not recalled"
        elif result.placement_low == result.placement_high:
            placement = str(result.placement_low)
        else:
            placement = f"{result.placement_low}-{result.placement_high}"
        _round_id, _round_order, highest_round = highest_round_by_key.get(
            (entry.id, comp_event.id)
        ) or _highest_round_fallback(session, comp_event.id, result.placement_low)
        rows.append(
            {
                "Date": competition.start_date,
                "Competition": competition.name,
                "Event": comp_event.raw_title,
                "Style": comp_event.style,
                "Highest round": highest_round,
                "Placement": placement,
                "Field size": result.field_size,
                "Start #": entry.competitor_no,
            }
        )
    return pd.DataFrame(rows)


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
    parts = []
    for pid in (partnership.leader_id, partnership.follower_id):
        if pid is not None:
            p = session.get(Person, pid)
            if p is not None:
                parts.append(p.display_name)
    label = " & ".join(parts) if parts else "(solo)"
    return f"{label} [{partnership.kind}]"


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


def _person_names(session, person_ids: set[int]) -> dict[int, str]:
    if not person_ids:
        return {}
    rows = session.execute(select(Person.id, Person.display_name).where(Person.id.in_(person_ids))).all()
    return {pid: name for pid, name in rows}


def _ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def results_for_comp_event(session, comp_event_id: int) -> pd.DataFrame:
    stmt = (
        select(Result, Entry, Partnership)
        .select_from(Result)
        .join(Entry, Result.entry_id == Entry.id)
        .join(Partnership, Entry.partnership_id == Partnership.id)
        .where(Result.comp_event_id == comp_event_id)
    )
    rows = session.execute(stmt).all()

    person_ids = {pid for _, _, partnership in rows for pid in (partnership.leader_id, partnership.follower_id) if pid is not None}
    names = _person_names(session, person_ids)
    highest_round_by_key = _highest_round_labels_for_entries(session, [(entry.id, comp_event_id) for _, entry, _ in rows])

    prelim = []
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
            (entry.id, comp_event_id)
        ) or _highest_round_fallback(session, comp_event_id, result.placement_low)
        prelim.append((couple, highest_round, placement, result.field_size, entry.competitor_no, round_id, round_order, entry.id))

    # Batch-fetch how many judges marked each not-recalled couple in the
    # specific round they were eliminated in, so they can be ranked by how
    # close they got (see marks_totals_for_entries_in_round) instead of in
    # arbitrary order within the same round.
    entry_round_pairs = {(entry_id, round_id) for *_rest, round_id, _ro, entry_id in prelim if round_id is not None}
    marks_totals = marks_totals_for_entries_in_round(session, entry_round_pairs)

    out = []
    for couple, highest_round, placement, field_size, competitor_no, round_id, round_order, entry_id in prelim:
        out.append(
            {
                "Couple": couple,
                "Highest round": highest_round,
                "Placement": placement,
                "Field size": field_size,
                "Start #": competitor_no,
                "_round_order": round_order,
                "_marks_total": marks_totals.get((entry_id, round_id), 0) if round_id is not None else 0,
                "_first_name": (couple.split(" & ")[0].split() or [""])[0],
                "_entry_id": entry_id,
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
    return df


# Standard Latin syllabus order (matches the "CC,S,R,PD,J" abbreviations in
# event titles), not alphabetical -- used to order dances in the marks
# detail and routine-totals tables the way a dancer actually expects them.
_LATIN_DANCE_ORDER = ["Cha Cha", "Samba", "Rumba", "Paso Doble", "Jive"]


def _dance_sort_key(dance_name: str) -> tuple[int, str]:
    name = dance_name or ""
    for i, keyword in enumerate(_LATIN_DANCE_ORDER):
        if keyword.lower() in name.lower():
            return (i, name)
    return (len(_LATIN_DANCE_ORDER), name)


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


def competition_search(session) -> None:
    term = st.text_input("Search for a competition by name", "", key="competition_search_term")
    if not term:
        st.info("Type a competition name above (e.g. 'Emerald Ball', 'US National').")
        return

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

    st.header(competition.name)
    cols = st.columns(4)
    date_range = str(competition.start_date)
    if competition.end_date and competition.end_date != competition.start_date:
        date_range += f" - {competition.end_date}"
    cols[0].metric("Dates", date_range)
    cols[1].metric("Location", ", ".join(p for p in (competition.city, competition.country) if p) or "unknown")
    cols[2].metric("Sanctioning body", competition.sanctioning_body or "unknown")
    cols[3].metric("Source", competition.source)

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
    event_choice = st.selectbox("Select an event to view results", list(event_options.keys()))
    event = event_options[event_choice]

    df = results_for_comp_event(session, event.id)
    if df.empty:
        st.write("No results on file for this event.")
    else:
        display_cols = [c for c in df.columns if not c.startswith("_")]
        st.dataframe(df, use_container_width=True, hide_index=True, column_order=display_cols)

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


def dancer_search(session) -> None:
    term = st.text_input("Search for a dancer by name", "", key="dancer_search_term")
    if not term:
        st.info("Type a name above to search (matches display name and any known alias spelling).")
        return

    people = search_people(session, term)
    if not people:
        st.warning(f"No one found matching {term!r}.")
        return

    if len(people) == 1:
        person = people[0]
    else:
        options = {f"{p.display_name} ({p.country or 'unknown country'})  -- id {p.id}": p for p in people}
        choice = st.selectbox("Multiple matches -- pick one:", list(options.keys()))
        person = options[choice]

    st.header(person.display_name)
    cols = st.columns(3)
    cols[0].metric("Country", person.country or "unknown")
    cols[1].metric("Role", "Adjudicator" if person.is_adjudicator else "Competitor")
    cols[2].metric("External id on file", "yes" if person.wdsf_min else "no")

    partnerships = partnerships_for_person(session, person.id)
    if not partnerships:
        st.warning("No partnerships/entries on file for this person yet.")
        return

    # Each partnership's results are pulled once, then partnerships are shown
    # most-active-first -- with dozens of Pro-Am students this keeps the page
    # navigable instead of one giant mixed table (per user feedback).
    partnership_dfs = [(p, result_history_for_partnership(session, p.id)) for p in partnerships]
    partnership_dfs.sort(key=lambda pair: len(pair[1]), reverse=True)

    total_results = sum(len(df) for _, df in partnership_dfs)
    st.subheader(f"Partnerships ({len(partnerships)}) -- {total_results} results total")

    for i, (partnership, df) in enumerate(partnership_dfs):
        label = partner_label(session, partnership)
        with st.expander(f"{label} -- {len(df)} results", expanded=(i == 0)):
            if df.empty:
                st.write("No results on file for this partnership.")
                continue

            st.dataframe(df, use_container_width=True, hide_index=True)

            best = best_results(df)
            if not best.empty:
                st.write("**Best results**")
                st.dataframe(best, use_container_width=True, hide_index=True)


def main() -> None:
    session = _session()
    st.title("DanceSport Wiki")

    mode = st.radio("Search by", ["Dancer", "Competition"], horizontal=True)
    if mode == "Dancer":
        dancer_search(session)
    else:
        competition_search(session)


if __name__ == "__main__":
    main()
