"""SQLModel models for the per-voter geography tables in D1.

Loader-only (``loader`` extra), like ``repeat_models``: the Worker and ``early_vote_sync`` import
the committed DDL in ``voter_geo_tables`` instead. The build always goes through
``model_validate``. ``VoterGeo`` validates without a table (a table model routes every field set
through SQLAlchemy's attribute instrumentation, ADR-011); ``VoterGeoRow`` is the table.
Validation errors name fields, never values. See docs/adr/015-voter-geography.md.
"""

from __future__ import annotations

import re

from pydantic import field_validator, model_validator
from sqlalchemy import Text
from sqlmodel import Field, SQLModel

from texas_turnout_scraper.voter_geo_tables import (
    FIPS_WIDTH,
    FLAGS_ALL,
    SPLITS_ALL,
    VUID_WIDTH,
)

_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")


def _digits(value: str, width: int) -> bool:
    return value.isascii() and value.isdigit() and len(value) == width


class VoterGeo(SQLModel):
    """Where one VUID's first live roster listing places it, by the stated precinct."""

    vuid: str = Field(primary_key=True, sa_type=Text)
    county_fips: str | None = Field(default=None, sa_type=Text)
    pctkey: str | None = Field(default=None, sa_type=Text)
    cd: str | None = Field(default=None, sa_type=Text)
    sd: str | None = Field(default=None, sa_type=Text)
    hd: str | None = Field(default=None, sa_type=Text)
    split_flag: int | None = None
    flags: int
    src_hash: int
    matched_at: str = Field(sa_type=Text)

    @field_validator("vuid")
    @classmethod
    def _vuid(cls, value: str) -> str:
        if not _digits(value, VUID_WIDTH):
            raise ValueError("not a VUID")
        return value

    @field_validator("county_fips")
    @classmethod
    def _county_fips(cls, value: str | None) -> str | None:
        if value is not None and not _digits(value, FIPS_WIDTH):
            raise ValueError("not a county FIPS code")
        return value

    @field_validator("flags")
    @classmethod
    def _flags(cls, value: int) -> int:
        if value & ~FLAGS_ALL:
            raise ValueError("unknown flag bits")
        return value

    @field_validator("matched_at")
    @classmethod
    def _matched_at(cls, value: str) -> str:
        if _TIMESTAMP.fullmatch(value) is None:
            raise ValueError("not a UTC timestamp")
        return value

    @model_validator(mode="after")
    def _split_follows_pctkey(self) -> VoterGeo:
        if (self.split_flag is None) != (self.pctkey is None):
            raise ValueError("split_flag is set exactly when pctkey is")
        if self.split_flag is not None and self.split_flag & ~SPLITS_ALL:
            raise ValueError("unknown split bits")
        return self


class VoterGeoRow(VoterGeo, table=True):
    """The ``voter_geo`` table; its DDL is the committed ``VOTER_GEO_TABLE_SQL``."""

    __tablename__ = "voter_geo"
    __table_args__ = {"sqlite_with_rowid": False}


class VoterGeoMeta(SQLModel, table=True):
    """What the last build saw. Written after every other voter_geo write."""

    __tablename__ = "voter_geo_meta"

    name: str = Field(primary_key=True, sa_type=Text)
    value: str = Field(sa_type=Text)
