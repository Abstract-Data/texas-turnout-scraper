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


class PoliticalParty(str, enum.Enum):
    """Political party affiliation recorded on a voter roster entry."""

    REPUBLICAN = "REPUBLICAN"
    DEMOCRATIC = "DEMOCRATIC"
    LIBERTARIAN = "LIBERTARIAN"
    GREEN = "GREEN"
    NONPARTISAN = "NONPARTISAN"
    UNKNOWN = "UNKNOWN"


def infer_election_type(name: str) -> ElectionType:
    """Infer an :class:`ElectionType` from an election name string.

    Matching is case-insensitive and uses substring checks applied in
    priority order so that more-specific patterns take precedence over
    shorter ones (e.g. ``"PRIMARY RUNOFF"`` is tested before ``"PRIMARY"``).

    Args:
        name: The election name as returned by the SOS portal (any case).

    Returns:
        The best-matching :class:`ElectionType`, or :attr:`ElectionType.UNKNOWN`
        when no pattern matches.
    """
    upper = name.upper()

    if "PRIMARY RUNOFF" in upper:
        return ElectionType.PRIMARY_RUNOFF
    if "PRIMARY" in upper:
        return ElectionType.PRIMARY
    if "GENERAL" in upper:
        return ElectionType.GENERAL
    if "SPECIAL RUNOFF" in upper:
        return ElectionType.SPECIAL
    if "SPECIAL" in upper:
        return ElectionType.SPECIAL
    if "CONSTITUTIONAL" in upper:
        return ElectionType.CONSTITUTIONAL_AMENDMENT
    if "LOCAL" in upper:
        return ElectionType.LOCAL
    return ElectionType.UNKNOWN
