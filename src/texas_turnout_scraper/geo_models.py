"""SQLModel models for the geography tables in D1.

Loader-only (``loader`` extra), like ``roster_models``: the Worker and ``early_vote_sync`` import
the committed DDL in ``geo_tables`` instead. See docs/adr/010-geography-tables.md.
"""

from __future__ import annotations

from pydantic import field_validator
from sqlmodel import Field, SQLModel

from texas_turnout_scraper.geo_tables import ALIAS_RULES, AREA_KINDS


class GeoArea(SQLModel, table=True):
    """One county, precinct or district, with a point inside its shape."""

    __tablename__ = "geo_area"

    kind: str = Field(primary_key=True)
    code: str = Field(primary_key=True)
    name: str
    lat: float
    lon: float

    @field_validator("kind")
    @classmethod
    def _kind(cls, value: str) -> str:
        if value not in AREA_KINDS:
            raise ValueError("unknown area kind")
        return value


class GeoPrecinct(SQLModel, table=True):
    """A state precinct and the district of each plan it overlaps most."""

    __tablename__ = "geo_precinct"

    pctkey: str = Field(primary_key=True)
    county_fips: str
    prec: str
    cd: str | None = None
    sd: str | None = None
    hd: str | None = None
    cd_share: float | None = None
    sd_share: float | None = None
    hd_share: float | None = None
    split_cd: bool | None = None
    split_sd: bool | None = None
    split_hd: bool | None = None

    @field_validator("county_fips")
    @classmethod
    def _fips(cls, value: str) -> str:
        if not (len(value) == 3 and value.isascii() and value.isdigit()):
            raise ValueError("not a 3-digit county FIPS code")
        return value


class GeoPrecinctAlias(SQLModel, table=True):
    """A roster (county, precinct) pair and the state precinct it resolves to, if any."""

    __tablename__ = "geo_precinct_alias"

    county: str = Field(primary_key=True)
    precinct: str = Field(primary_key=True)
    pctkey: str | None = None
    rule: str

    @field_validator("rule")
    @classmethod
    def _rule(cls, value: str) -> str:
        if value not in ALIAS_RULES:
            raise ValueError("unknown alias rule")
        return value


class GeoMeta(SQLModel, table=True):
    """Which geography file is loaded. Written after every other geography write."""

    __tablename__ = "geo_meta"

    name: str = Field(primary_key=True)
    sha256: str
