"""M2 success check (spec): "search a name, get a correct multi-event history."

Run with: streamlit run app.py
"""
from __future__ import annotations

import pandas as pd
import streamlit as st
from sqlalchemy import func, or_, select

from dsr.db import get_session
from dsr.models import CompEvent, Competition, Entry, Mark, Partnership, Person, PersonAlias, Result, Round

st.set_page_config(page_title="DanceSport Results", layout="wide")


@st.cache_resource
def _session():
    return get_session()


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
) -> dict[tuple[int, int], tuple[int, str]]:
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

    Returns (round_order, label) rather than just label, so callers that need
    to rank not-recalled couples by how far they got (Semi-Final > Quarter-
    Final > Round N > ... > Round 1) don't need a second query -- round_order
    is exactly the spec's "final has the highest round_order" ordering.
    """
    if not entry_comp_events:
        return {}
    entry_ids = {eid for eid, _ in entry_comp_events}
    rows = session.execute(
        select(Mark.entry_id, Round.comp_event_id, Round.round_order, Round.round_type)
        .join(Round, Mark.round_id == Round.id)
        .where(Mark.entry_id.in_(entry_ids))
        .distinct()
    ).all()
    best: dict[tuple[int, int], tuple[int, str]] = {}
    for entry_id, comp_event_id, round_order, round_type in rows:
        key = (entry_id, comp_event_id)
        if key not in best or round_order > best[key][0]:
            best[key] = (round_order, round_type)
    return {key: (ro, "Final" if rt == "final" else rt) for key, (ro, rt) in best.items()}


def _highest_round_fallback(session, comp_event_id: int, placement_low: int | None) -> tuple[int, str]:
    """Used only for the rare entry with no marks on record at all but a
    known placement (shouldn't normally happen for pipeline-loaded data)."""
    if placement_low is None:
        return (-1, "unknown")
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
            return (round_row.round_order, "Final" if round_row.round_type == "final" else round_row.round_type)
    return (-1, "unknown")


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

    rows = []
    for competition, comp_event, entry, result in result_rows:
        if result.placement_low is None:
            placement = "not recalled"
        elif result.placement_low == result.placement_high:
            placement = str(result.placement_low)
        else:
            placement = f"{result.placement_low}-{result.placement_high}"
        _round_order, highest_round = highest_round_by_key.get((entry.id, comp_event.id)) or _highest_round_fallback(
            session, comp_event.id, result.placement_low
        )
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
    scored = df[df["Placement"] != "not recalled"].copy()
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

    out = []
    for result, entry, partnership in rows:
        parts = [names[pid] for pid in (partnership.leader_id, partnership.follower_id) if pid in names]
        couple = " & ".join(parts) if parts else "(solo)"
        if result.placement_low is None:
            placement = "not recalled"
        elif result.placement_low == result.placement_high:
            placement = str(result.placement_low)
        else:
            placement = f"{result.placement_low}-{result.placement_high}"
        round_order, highest_round = highest_round_by_key.get((entry.id, comp_event_id)) or _highest_round_fallback(
            session, comp_event_id, result.placement_low
        )
        out.append(
            {
                "Couple": couple,
                "Highest round": highest_round,
                "Placement": placement,
                "Field size": result.field_size,
                "Start #": entry.competitor_no,
                "_round_order": round_order,
            }
        )
    df = pd.DataFrame(out)
    if not df.empty:
        # Placed couples sort by placement ascending, same as before. Couples
        # who weren't recalled sort after every placed couple, and among
        # themselves by how far they got -- Semi-Final before Quarter-Final
        # before Round N before Round N-1, etc (round_order descending) --
        # per user request, since "not recalled" alone doesn't distinguish a
        # semi-finalist from a first-round exit.
        not_recalled = df["Placement"] == "not recalled"
        df["_sort"] = pd.NA
        df.loc[~not_recalled, "_sort"] = df.loc[~not_recalled, "Placement"].str.split("-").str[0].astype(float)
        df.loc[not_recalled, "_sort"] = 1_000_000 - df.loc[not_recalled, "_round_order"]
        df["_sort"] = df["_sort"].astype(float)
        df = df.sort_values("_sort").drop(columns=["_sort", "_round_order"]).reset_index(drop=True)
    return df


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
    style_choice = st.selectbox("Filter by style", ["All"] + styles)
    filtered = [e for e in events if style_choice == "All" or (e.style or "unknown") == style_choice]
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
        st.dataframe(df, use_container_width=True, hide_index=True)

    st.subheader("Entries by style")
    style_counts: dict[str, int] = {}
    for e in events:
        key = e.style or "unknown"
        style_counts[key] = style_counts.get(key, 0) + counts.get(e.id, 0)
    if style_counts:
        st.bar_chart(pd.Series(style_counts))
    else:
        st.write("No results on file yet.")


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

    st.subheader("Event mix by style (all partnerships combined)")
    combined = pd.concat([df for _, df in partnership_dfs if not df.empty], ignore_index=True)
    if combined.empty:
        st.write("No competition results on file yet.")
    else:
        st.bar_chart(combined["Style"].fillna("unknown").value_counts())


def main() -> None:
    session = _session()
    st.title("DanceSport Results")

    mode = st.radio("Search by", ["Dancer", "Competition"], horizontal=True)
    if mode == "Dancer":
        dancer_search(session)
    else:
        competition_search(session)


if __name__ == "__main__":
    main()
