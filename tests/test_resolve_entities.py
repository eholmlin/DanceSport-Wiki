import pytest

from dsr.db import get_session, init_db
from dsr.parse.staging import StagingPersonRef
from dsr.resolve.entities import resolve_partnership, resolve_person


@pytest.fixture
def session(tmp_path):
    engine = init_db(tmp_path / "test.sqlite3")
    s = get_session(engine)
    yield s
    s.close()


def test_new_person_created_once(session):
    ref = StagingPersonRef(name="Ngoc An", external_ref="e76e95f4-a2c1-4ba2-88c8-a7e6002034e0")
    p1 = resolve_person(session, source="wdsf", ref=ref)
    session.commit()
    assert p1.id is not None
    assert p1.display_name == "Ngoc An"
    assert p1.wdsf_min == ref.external_ref


def test_same_external_ref_resolves_to_same_person_even_if_name_string_differs(session):
    ref_a = StagingPersonRef(name="Ngoc An", external_ref="e76e95f4-a2c1-4ba2-88c8-a7e6002034e0")
    ref_b = StagingPersonRef(name="An Ngoc", external_ref="e76e95f4-a2c1-4ba2-88c8-a7e6002034e0")  # order flipped
    p1 = resolve_person(session, source="wdsf", ref=ref_a)
    p2 = resolve_person(session, source="wdsf", ref=ref_b)
    session.commit()
    assert p1.id == p2.id


def test_exact_alias_hit_without_external_ref_reuses_person(session):
    ref = StagingPersonRef(name="Jose Macedo", external_ref=None)
    p1 = resolve_person(session, source="wdsf", ref=ref)
    p2 = resolve_person(session, source="wdsf", ref=ref)
    session.commit()
    assert p1.id == p2.id


def test_different_names_without_external_ref_create_different_people(session):
    p1 = resolve_person(session, source="wdsf", ref=StagingPersonRef(name="Alice Smith"))
    p2 = resolve_person(session, source="wdsf", ref=StagingPersonRef(name="Bob Jones"))
    session.commit()
    assert p1.id != p2.id


def test_resolve_is_idempotent_across_repeated_runs(session):
    ref = StagingPersonRef(name="Lan Yu Hsiang", external_ref="7f65b2cf-fa57-4505-8402-a5fc002ba7fe")
    ids = set()
    for _ in range(3):
        p = resolve_person(session, source="wdsf", ref=ref)
        session.commit()
        ids.add(p.id)
    assert len(ids) == 1


def test_partnership_resolution_is_idempotent(session):
    leader = resolve_person(session, source="wdsf", ref=StagingPersonRef(name="Ngoc An"))
    follower = resolve_person(session, source="wdsf", ref=StagingPersonRef(name="Le To Uyen"))
    session.commit()

    p1 = resolve_partnership(session, leader=leader, follower=follower, kind="amateur")
    p2 = resolve_partnership(session, leader=leader, follower=follower, kind="amateur")
    session.commit()
    assert p1.id == p2.id
