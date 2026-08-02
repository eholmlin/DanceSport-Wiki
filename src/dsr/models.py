"""SQLAlchemy ORM models mirroring docs/spec.md section 4.

SQLite-compatible for M0-M1 (per spec: SQLite acceptable early, Postgres later).
Array/JSONB columns use the cross-dialect `JSON` type; Postgres can be swapped to
native ARRAY/JSONB later without touching call sites.
"""
from __future__ import annotations

import datetime as dt
from typing import Optional

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


# ---------- provenance ----------


class RawDocument(Base):
    __tablename__ = "raw_document"
    __table_args__ = (UniqueConstraint("source", "url", "content_sha256"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String, nullable=False)
    url: Mapped[str] = mapped_column(String, nullable=False)
    fetched_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    http_status: Mapped[Optional[int]]
    content_sha256: Mapped[str] = mapped_column(String, nullable=False)
    storage_path: Mapped[str] = mapped_column(String, nullable=False)
    parser_version: Mapped[Optional[str]]
    parsed_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))


# ---------- people & orgs ----------


class Person(Base):
    __tablename__ = "person"

    id: Mapped[int] = mapped_column(primary_key=True)
    display_name: Mapped[str] = mapped_column(String, nullable=False)
    given_name: Mapped[Optional[str]]
    family_name: Mapped[Optional[str]]
    wdsf_min: Mapped[Optional[str]] = mapped_column(String, unique=True)
    ndca_number: Mapped[Optional[str]] = mapped_column(String, unique=True)
    country: Mapped[Optional[str]]
    is_adjudicator: Mapped[bool] = mapped_column(Boolean, default=False)
    is_professional: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc)
    )


class PersonAlias(Base):
    __tablename__ = "person_alias"
    __table_args__ = (UniqueConstraint("raw_name", "source", "person_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    person_id: Mapped[Optional[int]] = mapped_column(ForeignKey("person.id"))
    raw_name: Mapped[str] = mapped_column(String, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)
    confidence: Mapped[Optional[float]] = mapped_column(Numeric(4, 3))
    resolved_by: Mapped[Optional[str]]  # 'auto' | 'human' | 'external_id'


class Organization(Base):
    __tablename__ = "organization"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[Optional[str]]  # 'studio' | 'collegiate' | 'club'
    city: Mapped[Optional[str]]
    state: Mapped[Optional[str]]
    country: Mapped[Optional[str]]


class OrganizationAlias(Base):
    __tablename__ = "organization_alias"

    organization_id: Mapped[int] = mapped_column(ForeignKey("organization.id"), primary_key=True)
    raw_name: Mapped[str] = mapped_column(String, primary_key=True)


class Affiliation(Base):
    __tablename__ = "affiliation"

    person_id: Mapped[int] = mapped_column(ForeignKey("person.id"), primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organization.id"), primary_key=True)
    first_seen: Mapped[Optional[dt.date]] = mapped_column(Date)
    last_seen: Mapped[Optional[dt.date]] = mapped_column(Date)


# ---------- the competing unit ----------


class Partnership(Base):
    __tablename__ = "partnership"
    __table_args__ = (UniqueConstraint("leader_id", "follower_id", "kind"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    leader_id: Mapped[Optional[int]] = mapped_column(ForeignKey("person.id"))
    follower_id: Mapped[Optional[int]] = mapped_column(ForeignKey("person.id"))
    kind: Mapped[str] = mapped_column(String, nullable=False)  # amateur|pro_am|professional|formation
    student_id: Mapped[Optional[int]] = mapped_column(ForeignKey("person.id"))
    first_seen: Mapped[Optional[dt.date]] = mapped_column(Date)
    last_seen: Mapped[Optional[dt.date]] = mapped_column(Date)


# ---------- competitions ----------


class Competition(Base):
    __tablename__ = "competition"
    __table_args__ = (UniqueConstraint("source", "source_code"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String, nullable=False)
    source_code: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    start_date: Mapped[Optional[dt.date]] = mapped_column(Date)
    end_date: Mapped[Optional[dt.date]] = mapped_column(Date)
    city: Mapped[Optional[str]]
    state: Mapped[Optional[str]]
    country: Mapped[Optional[str]]
    sanctioning_body: Mapped[Optional[str]]  # 'USA Dance' | 'NDCA' | 'WDSF' | 'collegiate'
    url: Mapped[Optional[str]]


class CompEvent(Base):
    __tablename__ = "comp_event"
    __table_args__ = (UniqueConstraint("competition_id", "raw_title"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    competition_id: Mapped[int] = mapped_column(ForeignKey("competition.id"))
    raw_title: Mapped[str] = mapped_column(String, nullable=False)
    style: Mapped[Optional[str]]  # Standard|Latin|Smooth|Rhythm|Nightclub|Country|Formation
    proficiency: Mapped[Optional[str]]  # Newcomer|Bronze|Silver|Gold|Novice|Pre-Champ|Champ|Open
    age_group: Mapped[Optional[str]]  # Juvenile|Junior|Youth|Adult|Senior I..IV
    role_class: Mapped[Optional[str]]  # amateur|pro_am|professional
    dances: Mapped[Optional[list]] = mapped_column(JSON)  # e.g. ['W','T','V','F','Q']
    is_scholarship: Mapped[bool] = mapped_column(Boolean, default=False)


class Entry(Base):
    __tablename__ = "entry"

    id: Mapped[int] = mapped_column(primary_key=True)
    competition_id: Mapped[int] = mapped_column(ForeignKey("competition.id"))
    partnership_id: Mapped[Optional[int]] = mapped_column(ForeignKey("partnership.id"))
    competitor_no: Mapped[Optional[str]]  # lead number, as printed
    organization_id: Mapped[Optional[int]] = mapped_column(ForeignKey("organization.id"))


class Round(Base):
    __tablename__ = "round"

    id: Mapped[int] = mapped_column(primary_key=True)
    comp_event_id: Mapped[int] = mapped_column(ForeignKey("comp_event.id"))
    round_type: Mapped[str] = mapped_column(String, nullable=False)  # prelim|redance|quarter|semi|final
    round_order: Mapped[int] = mapped_column(nullable=False)
    entries_in: Mapped[Optional[int]]
    recalled_count: Mapped[Optional[int]]


class Mark(Base):
    __tablename__ = "mark"
    __table_args__ = (UniqueConstraint("round_id", "entry_id", "judge_person_id", "dance"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    round_id: Mapped[int] = mapped_column(ForeignKey("round.id"))
    entry_id: Mapped[int] = mapped_column(ForeignKey("entry.id"))
    judge_person_id: Mapped[Optional[int]] = mapped_column(ForeignKey("person.id"))
    dance: Mapped[Optional[str]]
    recalled: Mapped[Optional[bool]] = mapped_column(Boolean)
    placement: Mapped[Optional[int]]


class Result(Base):
    __tablename__ = "result"

    comp_event_id: Mapped[int] = mapped_column(ForeignKey("comp_event.id"), primary_key=True)
    entry_id: Mapped[int] = mapped_column(ForeignKey("entry.id"), primary_key=True)
    placement_low: Mapped[Optional[int]]
    placement_high: Mapped[Optional[int]]
    made_final: Mapped[Optional[bool]] = mapped_column(Boolean)
    field_size: Mapped[Optional[int]]


# ---------- derived ----------


class PairwiseOutcome(Base):
    __tablename__ = "pairwise_outcome"

    id: Mapped[int] = mapped_column(primary_key=True)
    comp_event_id: Mapped[Optional[int]]
    winner_entry_id: Mapped[Optional[int]]
    loser_entry_id: Mapped[Optional[int]]
    round_id: Mapped[Optional[int]]
    weight: Mapped[Optional[float]] = mapped_column(Numeric)


class Rating(Base):
    __tablename__ = "rating"

    subject_type: Mapped[str] = mapped_column(String, primary_key=True)  # 'partnership' | 'person'
    subject_id: Mapped[int] = mapped_column(primary_key=True)
    style: Mapped[str] = mapped_column(String, primary_key=True)
    as_of: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    method: Mapped[str] = mapped_column(String, primary_key=True)  # bradley_terry|elo|glicko2
    mu: Mapped[Optional[float]] = mapped_column(Numeric)
    sigma: Mapped[Optional[float]] = mapped_column(Numeric)


class ResolutionQueue(Base):
    __tablename__ = "resolution_queue"

    id: Mapped[int] = mapped_column(primary_key=True)
    raw_name: Mapped[Optional[str]]
    source: Mapped[Optional[str]]
    candidate_person_id: Mapped[Optional[int]] = mapped_column(ForeignKey("person.id"))
    score: Mapped[Optional[float]] = mapped_column(Numeric)
    context: Mapped[Optional[dict]] = mapped_column(JSON)  # partner, org, comp, dates
    status: Mapped[str] = mapped_column(String, default="pending")  # pending|merged|new_person|rejected
    decided_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
