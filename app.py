"""M2 success check (spec): "search a name, get a correct multi-event history."

Run with: streamlit run app.py
"""
from __future__ import annotations

import pandas as pd
import streamlit as st
from sqlalchemy import or_, select

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


def _highest_round_labels_for_entries(session, entry_ids: list[int]) -> dict[int, str]:
    """Batched version: one query for every entry at once, instead of one
    query per row. With ~3M marks loaded, the old per-row query (a JOIN +
    ORDER BY + LIMIT 1 called once per result) made any dancer with a few
    hundred results take ages to render -- a real N+1 query bug, not a
    micro-optimization."""
    if not entry_ids:
        return {}
    rows = session.execute(
        select(Mark.entry_id, Round.round_order, Round.round_type)
        .join(Round, Mark.round_id == Round.id)
        .where(Mark.entry_id.in_(entry_ids))
        .distinct()
    ).all()
    best: dict[int, tuple[int, str]] = {}
    for entry_id, round_order, round_type in rows:
        if entry_id not in best or round_order > best[entry_id][0]:
            best[entry_id] = (round_order, round_type)
    return {eid: ("Final" if rt == "final" else rt) for eid, (_ro, rt) in best.items()}


def _highest_round_fallback(session, comp_event_id: int, placement_low: int | None) -> str:
    """Used only for the rare entry with no marks on record at all but a
    known placement (shouldn't normally happen for pipeline-loaded data)."""
    if placement_low is None:
        return "unknown"
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
            return "Final" if round_row.round_type == "final" else round_row.round_type
    return "unknown"


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
    highest_round_by_entry = _highest_round_labels_for_entries(session, [entry.id for _, _, entry, _ in result_rows])

    rows = []
    for competition, comp_event, entry, result in result_rows:
        if result.placement_low is None:
            placement = "not recalled"
        elif result.placement_low == result.placement_high:
            placement = str(result.placement_low)
        else:
            placement = f"{result.placement_low}-{result.placement_high}"
        highest_round = highest_round_by_entry.get(entry.id) or _highest_round_fallback(
            session, comp_event.id, result.placement_low
        )
        rows.append(
            {
                "Date": competition.start_date,
                "Competition": competition.name,
                "Event": comp_event.raw_title,
                "Style": comp_event.style,
                "Placement": placement,
                "Field size": result.field_size,
                "Highest round": highest_round,
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


def main() -> None:
    session = _session()
    st.title("DanceSport Results")

    term = st.text_input("Search for a dancer by name", "")
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


if __name__ == "__main__":
    main()
