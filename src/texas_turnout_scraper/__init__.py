"""Texas SOS early-voting turnout scraper.

Two data sources:
- Civix EVR (2025+): civix module — stateless GET, JSON/CSV responses
- Legacy SOS (pre-2025): session, elections, roster, turnout modules — stateful session

Both produce the same shared output models: VoterRecord, CountyRoster, CountyTurnout, AuditReport.
"""

from __future__ import annotations

__version__ = "0.2.2"

# Enums
# Civix client
# Writer / accumulation
from .audit import audit_records
from .civix import CivixClient
from .enums import ElectionType, VoteMethod, infer_election_type
from .legacy_api import (
    fetch_county_turnout,
    fetch_roster,
    fetch_single_county_roster,
)
from .legacy_api import (
    list_elections as list_legacy_elections,
)

# Shared models
# Civix models
# Legacy models
# Voterfile matching
from .models import (
    AuditFinding,
    AuditReport,
    CivixCountyRef,
    CivixCountyTurnout,
    CivixElection,
    CivixElectionDate,
    ColumnMapping,
    CountyRoster,
    CountyTurnout,
    EnrichedVoterRecord,
    LegacyElection,
    LegacyEVDate,
    VoterfileMatchReport,
    VoterRecord,
)
from .voterfile import (
    age_bracket,
    detect_columns,
    load_mapping,
    match_voterfile_to_roster,
    save_mapping,
    write_enriched_csv,
    write_match_report_json,
)
from .writer import (
    accumulate_roster,
    read_roster_csv,
    roster_csv_to_text,
    write_roster_csv,
)

__all__ = [
    "AuditFinding",
    "AuditReport",
    "CivixClient",
    "CivixCountyRef",
    "CivixCountyTurnout",
    "CivixElection",
    "CivixElectionDate",
    "ColumnMapping",
    "CountyRoster",
    "CountyTurnout",
    "ElectionType",
    "EnrichedVoterRecord",
    "LegacyEVDate",
    "LegacyElection",
    "VoteMethod",
    "VoterRecord",
    "VoterfileMatchReport",
    "__version__",
    "accumulate_roster",
    "age_bracket",
    "audit_records",
    "detect_columns",
    "fetch_county_turnout",
    "fetch_roster",
    "fetch_single_county_roster",
    "infer_election_type",
    "list_legacy_elections",
    "load_mapping",
    "match_voterfile_to_roster",
    "read_roster_csv",
    "roster_csv_to_text",
    "save_mapping",
    "write_enriched_csv",
    "write_match_report_json",
    "write_roster_csv",
]
