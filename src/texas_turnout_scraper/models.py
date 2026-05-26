"""Pydantic v2 models for the texas-turnout-scraper package.

Defines all data models used by the scraper, CLI, and MCP server.
No SQLModel, SQLAlchemy, or election_utils dependencies.
All models are database-agnostic and designed for Pydantic v2.

Models are grouped into three sections:
- Shared models (used by both Civix and Legacy sources)
- Civix-specific models (EVR API)
- Legacy-specific models (SOS HTML portal)
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .enums import ElectionType, VoteMethod, infer_election_type


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Shared models
# ---------------------------------------------------------------------------


class VoterRecord(BaseModel):
    """A single voter's record from a roster CSV.

    ``voter_name`` is stored only for duplicate name-mismatch detection across
    appearances; it is PII and must not be logged or returned from MCP tools.
    ``id_voter`` is always a 10-digit string; never coerce to int.

    Duplicate detection fields are populated by ``accumulate_roster()`` after all
    EV dates have been fetched. Raw records from a single fetch have these fields
    at their defaults (False / "").

    One file per election is written containing all EV dates combined. Each
    appearance is its own row — duplicates are flagged, not collapsed.
    """

    model_config = ConfigDict(frozen=False, extra="forbid")

    id_voter: str  # 10-digit Texas VUID string — NEVER int
    voting_method: VoteMethod
    precinct: str
    county: str  # county name (all-caps)
    election_id: str  # source_election_id (always str)
    report_date: date  # the EV date this record was fetched for

    # Duplicate detection — populated by accumulate_roster()
    # voter_name is stored ONLY to enable name-mismatch detection across appearances;
    # it must not be logged or included in MCP/API responses.
    voter_name: str = ""  # raw SOS name string — PII, for mismatch detection only

    duplicate_flag: bool = False
    duplicate_type: str = ""  # comma-sep flags, any of:
    #   "multiple_dates"     — same VUID on >1 report date
    #   "conflicting_method" — same VUID with IN-PERSON + MAIL-IN
    #   "multiple_counties"  — same VUID in >1 county
    #   "name_mismatch"      — same VUID but different VOTER_NAME
    #   "precinct_mismatch"  — same VUID but different PRECINCT
    also_found_on: str = ""  # semicolon-sep "COUNTY|YYYY-MM-DD" pairs for other appearances

    @classmethod
    def from_csv_row(
        cls,
        row: dict[str, str],
        *,
        county: str,
        election_id: str,
        report_date: date,
    ) -> VoterRecord:
        """Build a roster record from a Civix or legacy CSV row dict."""
        raw_id = row.get("ID_VOTER", "").strip()
        return cls(
            id_voter=raw_id.zfill(10),
            voter_name=row.get("VOTER_NAME", "").strip(),
            precinct=row.get("PRECINCT", "").strip(),
            voting_method=_parse_voting_method(row.get("VOTING_METHOD", "")),
            county=county,
            election_id=election_id,
            report_date=report_date,
        )


def _parse_voting_method(raw: str) -> VoteMethod:
    upper = raw.upper().strip()
    if "MAIL" in upper:
        return VoteMethod.MAIL_IN
    try:
        return VoteMethod(upper)
    except ValueError:
        return VoteMethod.IN_PERSON


class CountyRoster(BaseModel):
    """Roster for one county on one EV date.

    Used as an intermediate container during per-date fetching.
    Call ``accumulate_roster()`` to merge multiple CountyRosters into a
    flagged list of VoterRecords suitable for writing to the election file.
    """

    model_config = ConfigDict(frozen=False)

    county: str
    county_id: int | None = None  # None for legacy source (no county_id)
    election_id: str  # source_election_id (always str)
    report_date: date
    source: str  # "civix" or "legacy"
    records: list[VoterRecord] = Field(default_factory=list)

    @property
    def total_voters(self) -> int:
        return len(self.records)

    @property
    def in_person_count(self) -> int:
        return sum(1 for r in self.records if r.voting_method is VoteMethod.IN_PERSON)

    @property
    def mail_in_count(self) -> int:
        return sum(1 for r in self.records if r.voting_method is VoteMethod.MAIL_IN)


class CountyTurnout(BaseModel):
    """County-level turnout summary for one EV date."""

    model_config = ConfigDict(frozen=False)

    election_id: str  # source_election_id
    report_date: date
    county: str
    county_id: int | None = None  # None for legacy source
    registered_voters: int
    in_person_votes_on_date: int
    total_in_person_votes: int  # cumulative through this date
    total_mail_votes: int  # cumulative through this date
    roster_available: bool = False
    source: str  # "civix" or "legacy"


class AuditFinding(BaseModel):
    """A single audit finding."""

    model_config = ConfigDict(frozen=False)

    finding_type: str
    county: str | None = None
    detail: str
    severity: str  # "error", "warning", "info"


class AuditReport(BaseModel):
    """Data quality audit report for one election + date."""

    model_config = ConfigDict(frozen=False)

    election_id: str
    report_date: date
    source: str
    total_records: int
    unique_vuids: int
    duplicate_vuid_count: int
    cross_method_duplicate_count: int
    findings: list[AuditFinding] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=_utc_now)
    audit_schema_version: str = "2.0"


# ---------------------------------------------------------------------------
# Civix-specific models
# ---------------------------------------------------------------------------


class CivixElectionDate(BaseModel):
    """One EV date entry from the Civix election index."""

    model_config = ConfigDict(frozen=False)

    date: date  # parsed from MM/DD/YYYY
    date_turnout_id: int

    @field_validator("date", mode="before")
    @classmethod
    def parse_mmddyyyy(cls, v: object) -> object:
        if isinstance(v, str) and "/" in v:
            return datetime.strptime(v, "%m/%d/%Y").date()
        return v


class CivixCountyRef(BaseModel):
    """County reference from Civix election index."""

    model_config = ConfigDict(frozen=False)

    county_id: int
    name: str  # all-caps county name


class CivixElection(BaseModel):
    """Election record from the Civix EVR API."""

    model_config = ConfigDict(frozen=False)

    source_election_id: str  # str(id) — always string, canonical key
    id: int  # raw Civix integer ID
    type: str  # "EV"
    election_date: date  # parsed from MM/DD/YYYY
    election_name: str
    election_type: ElectionType = ElectionType.UNKNOWN
    certified: bool
    early_voting_dates: list[CivixElectionDate]
    counties: list[CivixCountyRef]

    @field_validator("election_date", mode="before")
    @classmethod
    def parse_mmddyyyy(cls, v: object) -> object:
        if isinstance(v, str) and "/" in v:
            return datetime.strptime(v, "%m/%d/%Y").date()
        return v

    @model_validator(mode="after")
    def infer_type(self) -> CivixElection:
        if self.election_type == ElectionType.UNKNOWN:
            self.election_type = infer_election_type(self.election_name)
        return self

    @property
    def data_dir_name(self) -> str:
        return self.source_election_id


class CivixCountyTurnout(BaseModel):
    """County turnout summary from EVR_EARLYVOTING or EVR_ELECTIONDAYTURNOUT."""

    model_config = ConfigDict(frozen=False)

    election_id: str  # str(election_id)
    report_date: date
    county: str
    county_id: int
    registered_voters: int
    in_person_votes_on_date: int
    total_in_person_votes: int  # cumulative
    total_mail_votes: int  # cumulative
    roster_available: bool  # derived from voter_details_report field
    source: str = "civix"


# ---------------------------------------------------------------------------
# Voterfile matching models
# ---------------------------------------------------------------------------


class ColumnMapping(BaseModel):
    """Maps standard field names to actual column names in a user's voterfile.

    Used by the interactive voterfile match CLI to record which voterfile
    column corresponds to each standard field.  Saved as a JSON sidecar so
    the user doesn't have to re-map on subsequent runs.

    Fields that could not be detected or were skipped by the user are None.
    """

    model_config = ConfigDict(frozen=False)

    # Primary join key — required
    vuid: str | None = None  # column containing Texas VUID

    # District fields
    cd: str | None = None  # Congressional District column
    hd: str | None = None  # State House District column
    sd: str | None = None  # State Senate District column

    # Geographic
    county: str | None = None  # County column
    precinct: str | None = None  # Precinct column

    # Name fields (use full_name OR first_name + last_name)
    full_name: str | None = None
    first_name: str | None = None
    last_name: str | None = None

    # Demographics
    dob: str | None = None  # Date of birth — expects YYYYMMDD or YYYY-MM-DD
    sex: str | None = None
    hispanic: str | None = None

    # Registration
    status: str | None = None  # Voter status (V = active)

    # Source metadata
    voterfile_path: str | None = None  # path to the voterfile this mapping was built for
    created_at: str | None = None  # ISO timestamp


class EnrichedVoterRecord(BaseModel):
    """A VoterRecord enriched with demographic and district fields from a voterfile.

    Produced by ``voterfile.match_voterfile_to_roster()``.
    Contains all original VoterRecord fields plus joined voterfile columns.

    PII note: voter_name is inherited from VoterRecord — must not be logged.
    id_voter must not be logged.
    """

    model_config = ConfigDict(frozen=False)

    # Original EV roster fields
    id_voter: str
    voting_method: VoteMethod
    precinct: str
    county: str
    election_id: str
    report_date: date
    voter_name: str = ""
    duplicate_flag: bool = False
    duplicate_type: str = ""
    also_found_on: str = ""

    # Voterfile-enriched fields (None when voter not found in voterfile)
    in_voterfile: bool = False
    cd: str | None = None  # Congressional District
    hd: str | None = None  # State House District
    sd: str | None = None  # State Senate District
    vf_county: str | None = None  # County from voterfile (cross-check)
    vf_precinct: str | None = None  # Precinct from voterfile (cross-check)
    age_bracket: str | None = None  # "18-24", "25-34", ..., "75+"
    sex: str | None = None
    hispanic: str | None = None
    voter_status: str | None = None  # V=active, S=suspense, etc.


class VoterfileMatchReport(BaseModel):
    """Audit report from matching an EV roster against a voterfile.

    All counts are over VUID appearances (rows), not unique VUIDs, so a voter
    appearing on multiple EV dates is counted multiple times.

    PII note: no VUIDs or voter names appear in any field of this model.
    """

    model_config = ConfigDict(frozen=False)

    election_id: str
    report_date: date
    voterfile_path: str
    roster_path: str

    # Match summary
    total_roster_records: int
    total_voterfile_records: int | None = None  # full-file count; None if skipped
    matched_count: int  # roster records found in voterfile
    unmatched_count: int  # roster records NOT in voterfile
    match_rate: float  # matched / total_roster_records

    # Breakdowns (matched records only)
    by_cd: dict[str, int] = {}
    by_hd: dict[str, int] = {}
    by_sd: dict[str, int] = {}
    by_county: dict[str, int] = {}
    by_age_bracket: dict[str, int] = {}
    by_sex: dict[str, int] = {}
    by_voting_method: dict[str, int] = {}
    by_hispanic: dict[str, int] = {}

    # Audit findings
    findings: list[AuditFinding] = []
    generated_at: datetime = Field(default_factory=_utc_now)


# ---------------------------------------------------------------------------
# Legacy-specific models
# ---------------------------------------------------------------------------


class LegacyEVDate(BaseModel):
    """One EV date available for a legacy election."""

    model_config = ConfigDict(frozen=False)

    date: date
    label: str  # display label from the SOS portal


class LegacyElection(BaseModel):
    """Election record from the legacy SOS HTML portal."""

    model_config = ConfigDict(frozen=False)

    source_election_id: str  # SOS numeric ID string e.g. "49664"
    election_name: str
    election_type: ElectionType = ElectionType.UNKNOWN
    election_year: int | None = None
    ev_dates: list[LegacyEVDate] = Field(default_factory=list)

    @model_validator(mode="after")
    def infer_type(self) -> LegacyElection:
        if self.election_type == ElectionType.UNKNOWN:
            self.election_type = infer_election_type(self.election_name)
        return self
