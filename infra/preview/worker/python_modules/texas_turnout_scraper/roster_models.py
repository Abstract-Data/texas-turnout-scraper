"""SQLModel models for the roster rows written to D1, the parser that fills them, and the day load.

Loader-only (``loader`` extra): the Worker and ``early_vote_sync`` must not import this module at
import time. D1 has no SQLAlchemy engine, so the models give field rules and the committed DDL in
``early_vote_roster_rows``, not queries. See docs/adr/009-sqlmodel-roster-models.md.

Every cell of the file is kept: known columns map to fields, the rest go to ``extra``. A row that
fails validation becomes a ``RosterReject`` holding the whole original row. Reasons carry field
names and error types only, never cell values.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Container, Iterator
from dataclasses import dataclass, field
from typing import Protocol

from pydantic import ValidationError, field_validator
from sqlalchemy import JSON, Column
from sqlmodel import Field, SQLModel

from texas_turnout_scraper.early_vote_errors import EarlyVoteSyncError
from texas_turnout_scraper.early_vote_roster_rows import (
    COUNT_VISIBLE_SQL,
    ROWS_PER_STATEMENT,
    VERSION_SELECT_SQL,
    VOTING_METHODS,
    VUID_WIDTH,
    DayVersion,
    RosterSchemaError,
    RowKey,
    Statement,
    day_number,
    gc_statements,
    header_fields,
    insert_statements,
    keyed_lines,
    parse_day_version,
    recovery_statements,
    reject_statements,
    remove_statements,
    retire_statements,
    version_statement,
    visible_counts,
    visible_params,
)

logger = logging.getLogger(__name__)


class RosterRowBase(SQLModel):
    county: str
    voter_name: str
    id_voter: str
    voting_method: str
    precinct: str = ""
    extra: dict[str, str] = Field(default_factory=dict)

    @field_validator("county", "voter_name")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("blank")
        return value

    @field_validator("id_voter")
    @classmethod
    def _vuid(cls, value: str) -> str:
        text = value.strip()
        if not (text.isascii() and text.isdigit() and len(text) <= VUID_WIDTH):
            raise ValueError("not a VUID")
        return text.zfill(VUID_WIDTH)

    @field_validator("voting_method")
    @classmethod
    def _method(cls, value: str) -> str:
        method = value.strip().upper()
        if method not in VOTING_METHODS:
            raise ValueError("unknown voting method")
        return method


class RosterRow(RosterRowBase, table=True):
    __tablename__ = "early_vote_roster_row"
    __table_args__ = {"sqlite_with_rowid": False}

    day: int = Field(primary_key=True)
    row_hash: int = Field(primary_key=True)
    occ: int = Field(primary_key=True)
    extra: dict[str, str] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    added_in: int
    removed_in: int | None = None


class RosterReject(SQLModel, table=True):
    __tablename__ = "early_vote_roster_reject"
    __table_args__ = {"sqlite_with_rowid": False}

    day: int = Field(primary_key=True)
    row_hash: int = Field(primary_key=True)
    occ: int = Field(primary_key=True)
    added_in: int
    removed_in: int | None = None
    reason: str
    raw: dict[str, object] = Field(sa_column=Column(JSON, nullable=False))


class RosterVersion(SQLModel, table=True):
    """One row per day. Readers serve the day's rows visible at ``version``."""

    __tablename__ = "early_vote_roster_version"

    source_election_id: str = Field(primary_key=True)
    ev_date: str = Field(primary_key=True)
    version: int
    file_id: str
    sha256: str
    row_count: int
    reject_count: int


@dataclass
class ParsedBatch:
    rows: list[RosterRow] = field(default_factory=list)
    rejects: list[RosterReject] = field(default_factory=list)


def _reason(error: ValidationError) -> str:
    return ",".join(
        f"{'.'.join(str(part) for part in item['loc'])}:{item['type']}" for item in error.errors()
    )


def iter_row_keys(decoded: bytes) -> Iterator[RowKey]:
    """``(row_hash, occ)`` of every data line, without parsing or validating the cells."""
    header_fields(decoded)
    return (key for key, _ in keyed_lines(decoded))


def parse_roster_batches(
    decoded: bytes,
    *,
    day: int,
    added_in: int,
    wanted: Container[RowKey] | None = None,
    batch_rows: int = ROWS_PER_STATEMENT,
) -> Iterator[ParsedBatch]:
    """Validated rows and rejects in file order, ``batch_rows`` lines per batch.

    Only lines whose key is in ``wanted`` are validated (all lines when it is None). The header
    is checked now, so a bad file raises ``RosterSchemaError`` before any batch.
    """
    header, fields = header_fields(decoded)
    return _batches(decoded, header, fields, day, added_in, wanted, batch_rows)


def _batches(
    decoded: bytes,
    header: list[str],
    fields: dict[int, str],
    day: int,
    added_in: int,
    wanted: Container[RowKey] | None,
    batch_rows: int,
) -> Iterator[ParsedBatch]:
    batch = ParsedBatch()
    for key, cells in keyed_lines(decoded):
        if wanted is not None and key not in wanted:
            continue
        ids = {"day": day, "row_hash": key[0], "occ": key[1], "added_in": added_in}
        raw: dict[str, object] = dict(zip(header, cells, strict=False))
        if len(cells) != len(header):
            if len(cells) > len(header):
                raw["_overflow"] = cells[len(header) :]
            batch.rejects.append(
                RosterReject.model_validate({**ids, "reason": "column_count", "raw": raw})
            )
        else:
            known: dict[str, object] = dict(ids)
            extra: dict[str, str] = {}
            for position, cell in enumerate(cells):
                name = fields.get(position)
                if name is None:
                    extra[header[position]] = cell
                else:
                    known[name] = cell
            try:
                batch.rows.append(RosterRow.model_validate({**known, "extra": extra}))
            except ValidationError as error:
                batch.rejects.append(
                    RosterReject.model_validate({**ids, "reason": _reason(error), "raw": raw})
                )
        if len(batch.rows) + len(batch.rejects) == batch_rows:
            yield batch
            batch = ParsedBatch()
    if batch.rows or batch.rejects:
        yield batch


class RosterRowsStore(Protocol):
    def _query_rows(
        self, database_id: str, sql: str, params: list[str]
    ) -> list[dict[str, object]]: ...

    def _statements(self, database_id: str, statements: list[tuple[str, list[str]]]) -> None: ...


@dataclass(frozen=True)
class RosterDayResult:
    version: int
    rows: int
    rejects: int
    added_rows: int
    added_rejects: int
    removed: int
    full_replace: bool = False

    @property
    def added(self) -> int:
        return self.added_rows + self.added_rejects


def _run(store: RosterRowsStore, database_id: str, statements: tuple[Statement, ...]) -> None:
    """One statement per call, so no request carries more than one bounded statement."""
    for sql, params in statements:
        store._statements(database_id, [(sql, list(params))])  # noqa: SLF001


def _base_keys(base: bytes | None) -> set[RowKey] | None:
    if base is None:
        return None
    try:
        return set(iter_row_keys(base))
    except RosterSchemaError:
        return None


def load_roster_day(
    store: RosterRowsStore,
    database_id: str,
    election_id: str,
    ev_date: str,
    *,
    file_id: str,
    sha256: str,
    decoded: bytes,
    load_base: Callable[[str], bytes | None],
) -> RosterDayResult:
    """Bring a day's rows to ``decoded`` by writing only the lines that were added or removed.

    ``load_base(file_id)`` returns the bytes the current version was built from, or None when
    they are gone; then every live row is retired and the whole file is inserted. Readers of the
    current version see no change until the version row is written. Counts only in logs.
    """
    new_keys = set(iter_row_keys(decoded))
    day = day_number(ev_date)
    current = parse_day_version(
        store._query_rows(database_id, VERSION_SELECT_SQL, [election_id, ev_date])  # noqa: SLF001
    )
    old_keys: set[RowKey] | None = set()
    if current is not None:
        old_keys = _base_keys(load_base(current.file_id))
    if current is not None and old_keys == new_keys:
        _run(
            store,
            database_id,
            (
                version_statement(
                    election_id,
                    ev_date,
                    DayVersion(
                        current.version, file_id, sha256, current.row_count, current.reject_count
                    ),
                ),
            ),
        )
        return RosterDayResult(current.version, current.row_count, current.reject_count, 0, 0, 0)

    version = current.version if current is not None else 0
    target = version + 1
    _run(store, database_id, recovery_statements(day, version))
    full_replace = old_keys is None
    if old_keys is None:
        _run(store, database_id, retire_statements(day, version))
        added: set[RowKey] = new_keys
        removed: list[RowKey] = []
    else:
        added = new_keys - old_keys
        removed = sorted(old_keys - new_keys)
    added_rows = added_rejects = 0
    for batch in parse_roster_batches(decoded, day=day, added_in=target, wanted=added):
        added_rows += len(batch.rows)
        added_rejects += len(batch.rejects)
        _run(
            store,
            database_id,
            (
                *insert_statements(day, target, batch.rows),
                *reject_statements(day, target, batch.rejects),
            ),
        )
    _run(store, database_id, remove_statements(day, target, removed))

    rows, rejects = visible_counts(
        store._query_rows(  # noqa: SLF001
            database_id, COUNT_VISIBLE_SQL, [str(day), *visible_params(target)] * 2
        )
    )
    if rows + rejects != len(new_keys):
        # The stored rows no longer match the base file, so publishing would serve a wrong day.
        # Nothing is flipped; the next run clears the unpublished marks and tries again.
        raise EarlyVoteSyncError(
            f"roster rows for {ev_date} count {rows + rejects}, the file has {len(new_keys)}; "
            "version not published"
        )
    _run(
        store,
        database_id,
        (
            version_statement(
                election_id, ev_date, DayVersion(target, file_id, sha256, rows, rejects)
            ),
        ),
    )
    _run(store, database_id, gc_statements(day, target))
    return RosterDayResult(
        target, rows, rejects, added_rows, added_rejects, len(removed), full_replace
    )
