import pytest
from sqlalchemy import select

from dsr.db import get_session, init_db
from dsr.models import Partnership, Person, PersonAlias, ResolutionQueue
from dsr.parse.staging import StagingPersonRef
from dsr.resolve.entities import resolve_partnership, resolve_person
from dsr.resolve.queue import decide, list_pending


@pytest.fixture
def session(tmp_path):
    engine = init_db(tmp_path / "fuzzy.sqlite3")
    s = get_session(engine)
    yield s
    s.close()


def test_fresh_external_ref_never_triggers_fuzzy_path_even_if_names_collide(session):
    # Regression test: a *present but unmatched* external_ref conclusively
    # means "new person" -- it must never run the fuzzy path just because no
    # one has that id yet. Before this was fixed, almost every athlete's
    # first-ever appearance (which always has a fresh, as-yet-unmatched
    # external_ref) incorrectly went through fuzzy scoring and could pick up
    # a bogus resolution_queue entry against an unrelated same-blocking-key
    # person, purely because two different people happen to share a surname.
    chen_wei = resolve_person(session, source="wdsf", ref=StagingPersonRef(name="Chen Wei", external_ref="guid-a"))
    chen_shuaiqi = resolve_person(
        session, source="wdsf", ref=StagingPersonRef(name="Chen Shuaiqi", external_ref="guid-b")
    )
    session.commit()

    assert chen_wei.id != chen_shuaiqi.id
    assert list_pending(session) == []


def test_low_similarity_creates_new_person_no_queue_entry(session):
    resolve_person(session, source="wdsf", ref=StagingPersonRef(name="Alice Smith"))
    session.commit()

    p2 = resolve_person(session, source="wdsf", ref=StagingPersonRef(name="Bob Jones"))
    session.commit()

    assert p2.display_name == "Bob Jones"
    assert list_pending(session) == []


def test_ambiguous_name_match_creates_new_person_plus_queue_entry(session):
    # No external ref, no shared partner/country corroboration: a reordered
    # exact-token match alone (score 0.85) lands in the queue band, not
    # auto-merge -- per spec, never silently merge on name alone.
    original = resolve_person(session, source="wdsf", ref=StagingPersonRef(name="Ngoc An"))
    session.commit()

    new_person = resolve_person(session, source="wdsf", ref=StagingPersonRef(name="An Ngoc"))
    session.commit()

    assert new_person.id != original.id  # never silently merged

    pending = list_pending(session)
    assert len(pending) == 1
    entry = pending[0]
    assert entry.raw_name == "An Ngoc"
    assert entry.candidate_person_id == original.id
    assert 0.75 <= float(entry.score) < 0.93
    assert entry.context["new_person_id"] == new_person.id


def test_shared_partner_pushes_ambiguous_match_to_auto_merge(session):
    shared_partner = resolve_person(session, source="wdsf", ref=StagingPersonRef(name="Le To Uyen"))
    original = resolve_person(session, source="wdsf", ref=StagingPersonRef(name="Ngoc An"))
    session.commit()
    resolve_partnership(session, leader=original, follower=shared_partner, kind="amateur")
    session.commit()

    # Same reordered name, but this time the incoming record's partner is
    # already known to have partnered with `original` -- corroboration pushes
    # the score over the auto-merge threshold.
    merged = resolve_person(
        session,
        source="wdsf",
        ref=StagingPersonRef(name="An Ngoc"),
        partner_person_ids=frozenset({shared_partner.id}),
    )
    session.commit()

    assert merged.id == original.id
    assert list_pending(session) == []  # auto-merged, no human review needed

    aliases = session.scalars(
        select(PersonAlias).where(PersonAlias.person_id == original.id, PersonAlias.raw_name == "An Ngoc")
    ).all()
    assert len(aliases) == 1
    assert aliases[0].resolved_by == "auto"
    assert 0.93 <= float(aliases[0].confidence) <= 1.0


def test_queue_decide_merge_reassigns_partnerships_and_aliases(session):
    original = resolve_person(session, source="wdsf", ref=StagingPersonRef(name="Ngoc An"))
    partner = resolve_person(session, source="wdsf", ref=StagingPersonRef(name="Le To Uyen"))
    session.commit()
    partnership = resolve_partnership(session, leader=original, follower=partner, kind="amateur")
    session.commit()

    duplicate = resolve_person(session, source="wdsf", ref=StagingPersonRef(name="An Ngoc"))
    session.commit()
    pending = list_pending(session)
    assert len(pending) == 1
    queue_id = pending[0].id

    decide(session, queue_id, "merge")
    session.commit()

    # the duplicate person row is gone
    assert session.get(Person, duplicate.id) is None
    # its alias now points at the original person
    moved_alias = session.scalar(select(PersonAlias).where(PersonAlias.raw_name == "An Ngoc"))
    assert moved_alias.person_id == original.id
    # the pre-existing partnership involving `original` is untouched and still resolvable
    same_partnership = resolve_partnership(session, leader=original, follower=partner, kind="amateur")
    assert same_partnership.id == partnership.id
    # queue entry marked decided
    entry = session.get(ResolutionQueue, queue_id)
    assert entry.status == "merged"
    assert entry.decided_at is not None


def test_queue_decide_new_person_leaves_both_distinct(session):
    original = resolve_person(session, source="wdsf", ref=StagingPersonRef(name="Ngoc An"))
    session.commit()
    duplicate = resolve_person(session, source="wdsf", ref=StagingPersonRef(name="An Ngoc"))
    session.commit()
    queue_id = list_pending(session)[0].id

    decide(session, queue_id, "new_person")
    session.commit()

    assert session.get(Person, original.id) is not None
    assert session.get(Person, duplicate.id) is not None
    entry = session.get(ResolutionQueue, queue_id)
    assert entry.status == "new_person"


def test_deciding_an_already_decided_entry_raises(session):
    resolve_person(session, source="wdsf", ref=StagingPersonRef(name="Ngoc An"))
    resolve_person(session, source="wdsf", ref=StagingPersonRef(name="An Ngoc"))
    session.commit()
    queue_id = list_pending(session)[0].id

    decide(session, queue_id, "rejected")
    session.commit()
    with pytest.raises(ValueError):
        decide(session, queue_id, "merge")
