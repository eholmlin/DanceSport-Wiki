"""Source-agnostic staging shapes emitted by parsers.

Parsers are pure functions of raw bytes (+ parser_version) -> these dataclasses.
No DB or network access happens in this module or in any `parse_*` function.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class StagingPersonRef:
    name: str
    external_ref: Optional[str] = None  # source-internal stable id, e.g. WDSF athlete GUID


@dataclass
class StagingCompetition:
    source: str
    source_code: str
    name: str
    start_date: Optional[dt.date]
    end_date: Optional[dt.date]
    city: Optional[str]
    country: Optional[str]
    sanctioning_body: Optional[str]
    url: str


@dataclass
class StagingCompEventRef:
    """One discipline within a competition, as listed on the event page."""

    source_code: str  # e.g. "Open-Taipei-Adult-Latin-66537"
    raw_title: str
    ranking_url: str
    marks_url: str
    officials_url: str
    final_url: str


@dataclass
class StagingEntry:
    competitor_no: str
    country: Optional[str]
    partner_1: StagingPersonRef
    partner_2: Optional[StagingPersonRef]  # None for solo events
    couple_ref: Optional[str] = None  # source-internal stable couple id, if a couple


@dataclass
class StagingRound:
    round_type: str
    round_order: int  # 1 = first round danced
    entries_in: int
    recalled_count: Optional[int]


@dataclass
class StagingResult:
    competitor_no: str
    placement_low: Optional[int]
    placement_high: Optional[int]
    made_final: bool
    field_size: int


@dataclass
class RankingPage:
    is_solo: bool
    rounds: list[StagingRound] = field(default_factory=list)
    entries: list[StagingEntry] = field(default_factory=list)  # placed + excused
    results: list[StagingResult] = field(default_factory=list)  # placed only


@dataclass
class StagingOfficial:
    letter: str
    name: str
    country: Optional[str]
    external_ref: Optional[str] = None


@dataclass
class StagingMark:
    round_order: Optional[int]  # None for the aggregated final-round page
    competitor_no: str
    judge_letter: str
    dance: Optional[str]  # None when the source only publishes a combined placement
    recalled: Optional[bool]
    placement: Optional[int]
