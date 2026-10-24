"""SQLModel models for the roster rows written to D1, and the parser that fills them.

Loader-only (``loader`` extra): the Worker and ``early_vote_sync`` must not import this module at
import time. D1 has no SQLAlchemy engine, so the models give field rules and the committed DDL in
``early_vote_roster_rows``, not queries. See docs/adr/009-sqlmodel-roster-models.md.

Every cell of the file is kept: known columns map to fields, the rest go to ``extra``. A row that
fails validation becomes a ``RosterReject`` holding the whole original row. Reasons carry field
names and error types only, never cell values.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterator
from dataclasses import dataclass, field

from pydantic import ValidationError, field_validator
from sqlalchemy import JSON, Column
from sqlmodel import Field, SQLModel

from texas_turnout_scraper.early_vote_roster_rows import (
    ROWS_PER_STATEMENT,
    VOTING_METHODS,
    VUID_WIDTH,
    header_fields,
)


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

    file_id: str = Field(primary_key=True)
    row_id: int = Field(primary_key=True)
    extra: dict[str, str] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))


class RosterReject(SQLModel, table=True):
    __tablename__ = "early_vote_roster_reject"

    file_id: str = Field(primary_key=True)
    row_id: int = Field(primary_key=True)
    reason: str
    raw: dict[str, object] = Field(sa_column=Column(JSON, nullable=False))


class RosterRowsMarker(SQLModel, table=True):
    __tablename__ = "early_vote_roster_rows"

    source_election_id: str = Field(primary_key=True)
    file_id: str = Field(primary_key=True)
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


def _reject(file_id: str, row_id: int, reason: str, raw: dict[str, object]) -> RosterReject:
    return RosterReject.model_validate(
        {"file_id": file_id, "row_id": row_id, "reason": reason, "raw": raw}
    )


def parse_roster_batches(
    decoded: bytes, file_id: str, batch_rows: int = ROWS_PER_STATEMENT
) -> Iterator[ParsedBatch]:
    """Validated rows and rejects in file order, ``batch_rows`` lines per batch.

    The header is checked now, so a bad file raises ``RosterSchemaError`` before any batch.
    ``row_id`` counts every non-blank data line, rejected ones included.
    """
    header, fields = header_fields(decoded)
    return _batches(decoded, file_id, header, fields, batch_rows)


def _batches(
    decoded: bytes,
    file_id: str,
    header: list[str],
    fields: dict[int, str],
    batch_rows: int,
) -> Iterator[ParsedBatch]:
    reader = csv.reader(io.StringIO(decoded.decode("utf-8-sig")))
    next(reader)
    batch = ParsedBatch()
    row_id = 0
    for cells in reader:
        if not cells:
            continue
        raw: dict[str, object] = dict(zip(header, cells, strict=False))
        if len(cells) != len(header):
            if len(cells) > len(header):
                raw["_overflow"] = cells[len(header) :]
            batch.rejects.append(_reject(file_id, row_id, "column_count", raw))
        else:
            known: dict[str, object] = {"file_id": file_id, "row_id": row_id}
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
                batch.rejects.append(_reject(file_id, row_id, _reason(error), raw))
        row_id += 1
        if len(batch.rows) + len(batch.rejects) == batch_rows:
            yield batch
            batch = ParsedBatch()
    if batch.rows or batch.rejects:
        yield batch
