"""Enumerations for the texas-turnout-scraper package.

Defines election classification, vote method, and political party enums
using the ``str, enum.Enum`` pattern for full Pydantic v2 compatibility.
Also provides :func:`infer_election_type` for classifying elections by name.
"""

from __future__ import annotations

import enum


class ElectionType(str, enum.Enum):
    """High-level classification of a Texas election."""

    PRIMARY = "primary"
    PRIMARY_RUNOFF = "primary_runoff"
    GENERAL = "general"
    SPECIAL = "special"
    CONSTITUTIONAL_AMENDMENT = "constitutional_amendment"
    LOCAL = "local"
    UNKNOWN = "unknown"


class VoteMethod(str, enum.Enum):
    """Method by which a ballot was cast."""

    IN_PERSON = "IN-PERSON"
    MAIL_IN = "MAIL-IN"


class Source(str, enum.Enum):
    """Data source for roster and turnout artifacts."""

    CIVIX = "civix"
    LEGACY = "legacy"


class FindingType(str, enum.Enum):
    """Canonical audit finding types (schema 2.0)."""

    MULTIPLE_COUNTIES = "multiple_counties"
    CONFLICTING_METHOD = "conflicting_method"
    MULTIPLE_DATES = "multiple_dates"
    NAME_MISMATCH = "name_mismatch"
    PRECINCT_MISMATCH = "precinct_mismatch"
    TURNOUT_ANOMALY = "turnout_anomaly"
    MISSING_COUNTY = "missing_county"


_ELECTION_TYPE_PATTERNS: tuple[tuple[str, ElectionType], ...] = (
    ("primary runoff", ElectionType.PRIMARY_RUNOFF),
    ("primary", ElectionType.PRIMARY),
    ("general", ElectionType.GENERAL),
    ("special runoff", ElectionType.SPECIAL),
    ("special", ElectionType.SPECIAL),
    ("constitutional", ElectionType.CONSTITUTIONAL_AMENDMENT),
    ("local", ElectionType.LOCAL),
)


def infer_election_type(name: str) -> ElectionType:
    """Infer an :class:`ElectionType` from an election name string."""
    lower = name.lower()
    for needle, election_type in _ELECTION_TYPE_PATTERNS:
        if needle in lower:
            return election_type
    return ElectionType.UNKNOWN
