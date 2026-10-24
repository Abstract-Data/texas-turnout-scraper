"""SQLModel models for the repeat-VUID tables in D1.

Loader-only (``loader`` extra), like ``voter_models``: the Worker and ``early_vote_sync`` import the
committed DDL in ``repeat_tables`` instead. The build always goes through ``model_validate``.
``VuidRepeat`` validates without a table (a table model routes every field set through
SQLAlchemy's attribute instrumentation, ADR-011); ``VuidRepeatRow`` is the table. Validation
errors name fields, never values. See docs/adr/012-repeat-vuids.md.
"""

from __future__ import annotations

from datetime import date

from pydantic import field_validator
from sqlalchemy import JSON, Text
from sqlmodel import Field, SQLModel

from texas_turnout_scraper.early_vote_roster_rows import VOTING_METHODS
from texas_turnout_scraper.repeat_tables import FLAGS_ALL, VUID_WIDTH

_APPEARANCE_KEYS = ("day", "method", "county")


def _iso(value: str) -> str:
    date.fromisoformat(value)
    return value


def _ascending_dates(values: list[str]) -> list[str]:
    for value in values:
        _iso(value)
    if values != sorted(set(values)):
        raise ValueError("dates are not distinct and ascending")
    return values


class VuidRepeat(SQLModel):
    """A voter counted more than once among live roster rows, or removed at least once."""

    vuid: str = Field(primary_key=True, sa_type=Text)
    n_counted: int
    n_removed: int
    first_day: str | None = Field(default=None, sa_type=Text)
    counted_days: list[str] = Field(sa_type=JSON)
    appearances: list[dict[str, str]] = Field(sa_type=JSON)
    removed_days: list[str] = Field(sa_type=JSON)
    flags: int
    src_hash: int

    @field_validator("vuid")
    @classmethod
    def _vuid(cls, value: str) -> str:
        if not (value.isascii() and value.isdigit() and len(value) == VUID_WIDTH):
            raise ValueError("not a VUID")
        return value

    @field_validator("n_counted", "n_removed")
    @classmethod
    def _count(cls, value: int) -> int:
        if value < 0:
            raise ValueError("negative count")
        return value

    @field_validator("first_day")
    @classmethod
    def _first_day(cls, value: str | None) -> str | None:
        return None if value is None else _iso(value)

    @field_validator("counted_days", "removed_days")
    @classmethod
    def _days(cls, value: list[str]) -> list[str]:
        return _ascending_dates(value)

    @field_validator("appearances")
    @classmethod
    def _appearances(cls, value: list[dict[str, str]]) -> list[dict[str, str]]:
        for item in value:
            if tuple(item) != _APPEARANCE_KEYS:
                raise ValueError("appearance keys are not day, method, county")
            _iso(item["day"])
            if item["method"] not in VOTING_METHODS:
                raise ValueError("unknown voting method")
        return value

    @field_validator("flags")
    @classmethod
    def _flags(cls, value: int) -> int:
        if value & ~FLAGS_ALL:
            raise ValueError("unknown flag bits")
        return value


class VuidRepeatRow(VuidRepeat, table=True):
    """The ``early_vote_vuid_repeat`` table; its DDL is the committed ``REPEAT_TABLE_SQL``."""

    __tablename__ = "early_vote_vuid_repeat"
    __table_args__ = {"sqlite_with_rowid": False}


class RepeatExcess(SQLModel):
    """Non-first appearances among live rows at one day, county and voting method."""

    ev_date: str = Field(primary_key=True, sa_type=Text)
    county: str = Field(primary_key=True, sa_type=Text)
    voting_method: str = Field(primary_key=True, sa_type=Text)
    n: int

    @field_validator("ev_date")
    @classmethod
    def _ev_date(cls, value: str) -> str:
        return _iso(value)

    @field_validator("voting_method")
    @classmethod
    def _method(cls, value: str) -> str:
        if value not in VOTING_METHODS:
            raise ValueError("unknown voting method")
        return value

    @field_validator("n")
    @classmethod
    def _n(cls, value: int) -> int:
        if value < 1:
            raise ValueError("excess below one")
        return value


class RepeatExcessRow(RepeatExcess, table=True):
    __tablename__ = "early_vote_repeat_excess"


class RepeatMeta(SQLModel, table=True):
    """What the last build saw. Written after every other repeat write."""

    __tablename__ = "early_vote_repeat_meta"

    name: str = Field(primary_key=True, sa_type=Text)
    value: str = Field(sa_type=Text)
