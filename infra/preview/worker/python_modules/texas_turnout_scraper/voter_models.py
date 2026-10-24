"""SQLModel models for the voter attribute tables in D1, and the parser that fills them.

Loader-only (``loader`` extra), like ``roster_models``: the Worker and ``early_vote_sync`` import
the committed DDL in ``voter_tables`` instead. Ingest always goes through ``parse_voter``, which
calls ``Voter.model_validate``. A row that fails becomes a ``VoterReject`` holding the 21 selected
voterfile cells, with a reason made of field names and error types only. A bad DOB or EDR is not
a failure: it is stored as NULL with a bit set in ``flags``. See docs/adr/011-voter-attributes.md.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date

from pydantic import ValidationError, ValidationInfo, field_validator
from sqlalchemy import JSON, Column
from sqlmodel import Field, SQLModel

from texas_turnout_scraper.voter_scores import derive, src_hash
from texas_turnout_scraper.voter_tables import (
    DATED_KEYS,
    DATED_MODES,
    DOB_MIN_YEAR,
    EDR_MIN_YEAR,
    FLAG_DOB_INVALID,
    FLAG_EDR_INVALID,
    GENERAL_CODES,
    HISTORY_COLUMNS,
    PRIMARY_CODES,
    SCORE_VERSION,
    SEXES,
    VOTER_COLUMNS,
    VOTERFILE_COLUMNS,
    VUID_WIDTH,
)

_DATED_MODE_LETTERS = frozenset(DATED_MODES.values())


class Voter(SQLModel):
    """One voter who turned out in the current election: chosen attributes, history, scores.

    Validated without a table: a table model routes every field set through SQLAlchemy's
    attribute instrumentation, about half the cost of a link. ``VoterRow`` is the table.
    """

    vuid: str = Field(primary_key=True)
    sex: str
    dob: str | None = None
    edr: str | None = None
    flags: int = 0
    gen24: str | None = None
    gen23: str | None = None
    gen22: str | None = None
    gen21: str | None = None
    gen20: str | None = None
    gen19: str | None = None
    gen18: str | None = None
    pri26: str | None = None
    pri24: str | None = None
    pri22: str | None = None
    pri20: str | None = None
    pri18: str | None = None
    ro26: str | None = None
    ro24: str | None = None
    ro22: str | None = None
    ro20: str | None = None
    ro18: str | None = None
    gen25_day: int | None = None
    gen25_mode: str | None = None
    pri26_day: int | None = None
    ro26_day: int | None = None
    n_voted: int
    n_early: int
    n_absentee: int
    n_election_day: int
    n_gen_pres: int
    n_gen_mid: int
    n_gen_off: int
    n_primary_r: int
    n_primary_d: int
    primary_pattern: str
    general_pattern: str
    typical_mode: str | None = None
    early_or_absentee_share: float | None = None
    typical_earliness: float | None = None
    earliness_n: int
    score_version: int
    src_hash: int

    @field_validator("vuid")
    @classmethod
    def _vuid(cls, value: str) -> str:
        if not (value.isascii() and value.isdigit() and len(value) <= VUID_WIDTH):
            raise ValueError("not a VUID")
        return value.zfill(VUID_WIDTH)

    @field_validator("sex")
    @classmethod
    def _sex(cls, value: str) -> str:
        if value not in SEXES:
            raise ValueError("unknown sex code")
        return value

    @field_validator(*(c.lower() for c in HISTORY_COLUMNS))
    @classmethod
    def _code(cls, value: str | None, info: ValidationInfo) -> str | None:
        allowed = GENERAL_CODES if (info.field_name or "").startswith("gen") else PRIMARY_CODES
        if value is not None and value not in allowed:
            raise ValueError("unknown history code")
        return value

    @field_validator("dob", "edr")
    @classmethod
    def _iso(cls, value: str | None) -> str | None:
        if value is not None:
            date.fromisoformat(value)
        return value

    @field_validator("gen25_mode")
    @classmethod
    def _dated_mode(cls, value: str | None) -> str | None:
        if value is not None and value not in _DATED_MODE_LETTERS:
            raise ValueError("unknown early-voting mode")
        return value


class VoterRow(Voter, table=True):
    """The ``early_vote_voter`` table; its DDL is the committed ``VOTER_TABLE_SQL``."""

    __tablename__ = "early_vote_voter"
    __table_args__ = {"sqlite_with_rowid": False}


class VoterReject(SQLModel, table=True):
    """A voterfile row that failed validation, kept whole (the 21 selected columns)."""

    __tablename__ = "early_vote_voter_reject"
    __table_args__ = {"sqlite_with_rowid": False}

    vuid: str = Field(primary_key=True)
    reason: str
    raw: dict[str, str | None] = Field(sa_column=Column(JSON, nullable=False))


class ElectionCalendarRow(SQLModel, table=True):
    """An election whose early-voting days the portal lists; one row is the current election."""

    __tablename__ = "early_vote_election_calendar"

    election_key: str = Field(primary_key=True)
    election_date: str
    early_days: int
    first_early_date: str
    is_current: int


class AgeTier(SQLModel, table=True):
    __tablename__ = "age_tier"

    min_age: int
    max_age: int
    label: str = Field(primary_key=True)
    sort: int


class VoterMeta(SQLModel, table=True):
    """What the last link loaded. Written after every other voter write."""

    __tablename__ = "early_vote_voter_meta"

    name: str = Field(primary_key=True)
    value: str


def _clean(value: str | None) -> str | None:
    text = None if value is None else value.strip()
    return text or None


def _ymd(text: str | None, min_year: int, today: date) -> tuple[str | None, bool]:
    """``YYYYMMDD`` to an ISO date, or None; the flag says a value was there but not usable."""
    if text is None:
        return None, False
    if not (len(text) == 8 and text.isascii() and text.isdigit()):
        return None, True
    try:
        parsed = date(int(text[:4]), int(text[4:6]), int(text[6:]))
    except ValueError:
        return None, True
    if parsed.year < min_year or parsed > today:
        return None, True
    return parsed.isoformat(), False


def _reason(error: ValidationError) -> str:
    return ",".join(
        f"{'.'.join(str(part) for part in item['loc'])}:{item['type']}" for item in error.errors()
    )


def parse_voter(
    raw: Mapping[str, str | None],
    dated: Mapping[str, tuple[int, str] | None],
    *,
    today: date,
) -> Voter | VoterReject:
    """A validated voter, or a reject holding the selected cells. ``dated`` is keyed by election."""
    cells = {column: _clean(raw.get(column)) for column in VOTERFILE_COLUMNS}
    dob, dob_bad = _ymd(cells["DOB"], DOB_MIN_YEAR, today)
    edr, edr_bad = _ymd(cells["EDR"], EDR_MIN_YEAR, today)
    days = {key: dated.get(key) for key in DATED_KEYS}
    gen25, pri26, ro26 = (days[key] for key in ("GEN25", "PRI26", "RO26"))
    derived = derive({c: cells[c] for c in HISTORY_COLUMNS}, days)
    hashed = [SCORE_VERSION, *cells.values(), dob, edr]
    for entry in days.values():
        hashed += [None, None] if entry is None else list(entry)
    vuid = cells["VUID"] or ""
    try:
        return Voter.model_validate(
            {
                "vuid": vuid,
                "sex": cells["SEX"],
                "dob": dob,
                "edr": edr,
                "flags": (FLAG_DOB_INVALID if dob_bad else 0)
                | (FLAG_EDR_INVALID if edr_bad else 0),
                **{c.lower(): cells[c] for c in HISTORY_COLUMNS},
                "gen25_day": None if gen25 is None else gen25[0],
                "gen25_mode": None if gen25 is None else gen25[1],
                "pri26_day": None if pri26 is None else pri26[0],
                "ro26_day": None if ro26 is None else ro26[0],
                **derived.__dict__,
                "score_version": SCORE_VERSION,
                "src_hash": src_hash(hashed),
            }
        )
    except ValidationError as error:
        key = vuid.zfill(VUID_WIDTH) if vuid.isascii() and vuid.isdigit() else vuid
        return VoterReject.model_validate(
            {
                "vuid": key,
                "reason": _reason(error),
                "raw": {column: raw.get(column) for column in VOTERFILE_COLUMNS},
            }
        )


def voter_row(voter: Voter) -> list[object]:
    """The voter's values in ``VOTER_COLUMNS`` order, as the upsert payload carries them."""
    return [getattr(voter, column) for column in VOTER_COLUMNS]
