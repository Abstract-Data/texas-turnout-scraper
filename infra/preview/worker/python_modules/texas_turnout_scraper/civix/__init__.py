"""Civix clients: EVR roster API, ENR results API, and the static archive.

``from texas_turnout_scraper.civix import CivixClient`` remains the EVR entry
point. ENR and archive types are re-exported from this package.
"""

from __future__ import annotations

from ..models import CivixElection
from .archive import (
    CivixArchiveClient,
    CivixArchiveNotFoundError,
    discover_archive_election_refs,
    parse_constants_version,
    parse_election_version,
)
from .enr import (
    CENTRAL_TZ,
    discover_election_refs,
    fetch_county_info_payload,
    fetch_election_constants,
    fetch_election_payload,
    json_payload_hash,
    parse_civix_updated_time,
)
from .envelope import (
    _decode_envelope,
    decode_county_info_payload,
    decode_election_constants,
    decode_election_payload,
)
from .evr import (
    API_PREFIX,
    BASE_URL,
    DEFAULT_PACE_SECONDS,
    STATEWIDE_ED_POLLING_CSV,
    STATEWIDE_ED_VOTER_CSV,
    CivixClient,
    fetch_county_roster,
    parse_ed_statewide_voter_records,
)
from .models import (
    CivixArchiveElectionBundle,
    CivixArchiveElectionVersion,
    CivixCountyInfoPayload,
    CivixElectionPayload,
    CivixElectionRef,
    CountyInfoPayload,
    ElectionPayload,
)
from .read_client import EarlyVoteReadClient
from .transport import DEFAULT_ARCHIVE_BASE_URL, DEFAULT_ENR_BASE_URL

__all__ = [
    "API_PREFIX",
    "BASE_URL",
    "CENTRAL_TZ",
    "DEFAULT_ARCHIVE_BASE_URL",
    "DEFAULT_ENR_BASE_URL",
    "DEFAULT_PACE_SECONDS",
    "STATEWIDE_ED_POLLING_CSV",
    "STATEWIDE_ED_VOTER_CSV",
    "CivixArchiveClient",
    "CivixArchiveElectionBundle",
    "CivixArchiveElectionVersion",
    "CivixArchiveNotFoundError",
    "CivixClient",
    "CivixCountyInfoPayload",
    "CivixElection",
    "CivixElectionPayload",
    "CivixElectionRef",
    "CountyInfoPayload",
    "EarlyVoteReadClient",
    "ElectionPayload",
    "_decode_envelope",
    "decode_county_info_payload",
    "decode_election_constants",
    "decode_election_payload",
    "discover_archive_election_refs",
    "discover_election_refs",
    "fetch_county_info_payload",
    "fetch_county_roster",
    "fetch_election_constants",
    "fetch_election_payload",
    "json_payload_hash",
    "parse_civix_updated_time",
    "parse_constants_version",
    "parse_ed_statewide_voter_records",
    "parse_election_version",
]
