"""SQLModel models for the per-voter geography tables in D1.

Loader-only (``loader`` extra), like ``repeat_models``: the Worker and ``early_vote_sync`` import
the committed DDL in ``voter_geo_tables`` instead. The build always goes through
``model_validate``. ``VoterGeo`` validates without a table (a table model routes every field set
through SQLAlchemy's attribute instrumentation, ADR-011); ``VoterGeoRow`` is the table.
Validation errors name fields, never values. ``VoterGeoPoint`` is the Stage 2 residential point
(only the local ``voter_geo`` module writes it); its table carries the ADR-015 §8 CHECKs.
See docs/adr/015-voter-geography.md.
"""

from __future__ import annotations

import math
import re

from pydantic import field_validator, model_validator
from sqlalchemy import CheckConstraint, Text
from sqlmodel import Field, SQLModel

from texas_turnout_scraper.voter_geo_tables import (
    FIPS_WIDTH,
    FLAGS_ALL,
    GEOCODE_QUALITY_POINT,
    GEOCODE_SOURCE_POINT,
    SPLITS_ALL,
    VUID_WIDTH,
)

_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


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


class VoterGeoPoint(SQLModel):
    """A voter's residential address point and the districts under it.

    Only matched points exist, so ``lat`` and ``lon`` are always set; the columns are nullable
    because the table says "set exactly when the quality is ``address_point``" as a CHECK.
    """

    vuid: str = Field(primary_key=True, sa_type=Text)
    lat: float | None = None
    lon: float | None = None
    cd: str | None = Field(default=None, sa_type=Text)
    sd: str | None = Field(default=None, sa_type=Text)
    hd: str | None = Field(default=None, sa_type=Text)
    geocode_source: str = Field(sa_type=Text)
    geocode_quality: str = Field(sa_type=Text)
    address_points_vintage: str = Field(sa_type=Text)
    src_hash: int
    matched_at: str = Field(sa_type=Text)

    @field_validator("vuid")
    @classmethod
    def _vuid(cls, value: str) -> str:
        if not _digits(value, VUID_WIDTH):
            raise ValueError("not a VUID")
        return value

    @field_validator("lat")
    @classmethod
    def _lat(cls, value: float | None) -> float | None:
        if value is not None and not (math.isfinite(value) and -90 <= value <= 90):
            raise ValueError("latitude out of range")
        return value

    @field_validator("lon")
    @classmethod
    def _lon(cls, value: float | None) -> float | None:
        if value is not None and not (math.isfinite(value) and -180 <= value <= 180):
            raise ValueError("longitude out of range")
        return value

    @field_validator("geocode_source")
    @classmethod
    def _source(cls, value: str) -> str:
        if value != GEOCODE_SOURCE_POINT:
            raise ValueError("unknown geocode source")
        return value

    @field_validator("geocode_quality")
    @classmethod
    def _quality(cls, value: str) -> str:
        if value != GEOCODE_QUALITY_POINT:
            raise ValueError("unknown geocode quality")
        return value

    @field_validator("address_points_vintage")
    @classmethod
    def _vintage(cls, value: str) -> str:
        if _DATE.fullmatch(value) is None:
            raise ValueError("not a vintage date")
        return value

    @field_validator("matched_at")
    @classmethod
    def _matched_at(cls, value: str) -> str:
        if _TIMESTAMP.fullmatch(value) is None:
            raise ValueError("not a UTC timestamp")
        return value

    @model_validator(mode="after")
    def _point_is_whole(self) -> VoterGeoPoint:
        if self.lat is None or self.lon is None:
            raise ValueError("an address point has both lat and lon")
        return self


class VoterGeoPointRow(VoterGeoPoint, table=True):
    """The ``voter_geo_point`` table; its DDL is the committed ``VOTER_GEO_POINT_TABLE_SQL``."""

    __tablename__ = "voter_geo_point"
    __table_args__ = (
        CheckConstraint(
            f"(lat IS NOT NULL AND lon IS NOT NULL) = (geocode_quality = '{GEOCODE_QUALITY_POINT}')"
        ),
        CheckConstraint(f"geocode_quality = '{GEOCODE_QUALITY_POINT}'"),
        CheckConstraint(f"geocode_source = '{GEOCODE_SOURCE_POINT}'"),
        {"sqlite_with_rowid": False},
    )


class VoterGeoPointMeta(SQLModel, table=True):
    """What the last point build saw. Written after every other voter_geo_point write."""

    __tablename__ = "voter_geo_point_meta"

    name: str = Field(primary_key=True, sa_type=Text)
    value: str = Field(sa_type=Text)
