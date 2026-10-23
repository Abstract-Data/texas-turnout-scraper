"""Daily Civix early-vote sync into one D1 database per election.

The GitHub Action pulls one ``EVR_EARLYVOTING`` file, unwraps the
``{"upload": "<base64>"}`` envelope, and SHA-256-hashes those decoded bytes.
A matching stored hash exits without a write. A new file is staged under a
new ``file_id``: every chunk is inserted before the pointer moves. One
``INSERT ... ON CONFLICT`` then flips ``early_vote_pointer``. KV
``ev:<election>:version`` bumps only after that flip. The previous complete
file stays through KV propagation. Deleting it in this run would leave an
edge reader that still holds the old hash with no complete file, and a
reader mid-chunk could see a partial set. Stale ``file_id`` values — any id
that is neither the current pointer target nor the file whose SHA-256 matches
the stored KV hash — are deleted at the start of the next ``write_database``,
from D1 rows, not from process memory.

Readers — the Worker and the package read client — call
:func:`choose_published_file` before touching chunk rows. Trust a file only
when ``early_vote_file.chunk_count`` equals the number of chunk rows for that
``file_id`` and the file's SHA-256 equals the KV hash stored for the bumped
version. Until both are true, keep the previous complete file. A partial
stage is not served. KV writes can take about 60 seconds to show up at every
edge. That is not fit for election-night results. This module does not add a
faster path.

Cloudflare documents a 10 GB maximum D1 database size and says the cap cannot
be raised (https://developers.cloudflare.com/d1/platform/limits/). A larger
load raises :class:`EarlyVoteCapError` before any database is created.

KV is eventually consistent. Writes can take about 60 seconds to show up away
from the datacenter that accepted them
(https://developers.cloudflare.com/kv/concepts/how-kv-works/). That delay is
not fit for election-night results. This module does not add a faster path.

HTTP shapes used here, with ``Authorization: Bearer`` API tokens:

- D1 list: ``GET /accounts/{account_id}/d1/database``
- D1 create: ``POST /accounts/{account_id}/d1/database``
- D1 query: ``POST /accounts/{account_id}/d1/database/{database_id}/query``
  body ``{"sql", "params"}`` or ``{"batch": [{"sql", "params"}, ...]}``
  (https://developers.cloudflare.com/api/resources/d1/subresources/database/methods/query/)
- KV write: ``PUT /accounts/{account_id}/storage/kv/namespaces/{namespace_id}/values/{key_name}``
  with an ``application/octet-stream`` body. Colons in the key are percent-encoded
  (https://developers.cloudflare.com/api/resources/kv/subresources/namespaces/subresources/values/methods/update/).

``source_election_id`` stays a string. This module never logs voter names or VUIDs.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import os
import sys
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING, Any, Literal, Protocol

if TYPE_CHECKING:
    from texas_turnout_scraper.early_vote_d1_precompute_backfill import (
        PrecomputeBackfillResult,
    )
from urllib.parse import quote

import httpx

from texas_turnout_scraper.civix.envelope import _decode_envelope
from texas_turnout_scraper.civix.evr import API_PREFIX, BASE_URL
from texas_turnout_scraper.civix.ids import require_election_id
from texas_turnout_scraper.early_vote_d1_precompute_store import (
    PRECOMPUTE_SCHEMA_CREATES,
    aggregate_body_from_decoded,
    delete_precompute_statements,
    meta_upsert_statement,
    precompute_row_count,
    precompute_statements,
)
from texas_turnout_scraper.early_vote_day_changes import (
    DAY_CHANGE_TABLE_SQL,
    change_statements,
    diff_rosters,
    roster_row_count,
)
from texas_turnout_scraper.early_vote_roster import RosterCsvSummary, redact_roster_csv
from texas_turnout_scraper.http_transport import HttpBackend, PacedHttpClient
from texas_turnout_scraper.r2_snapshots import R2SnapshotStore, snapshot_store_from_env

logger = logging.getLogger(__name__)

# Civix EVR host only — polite scrape policy (see internal/evr-legal-guardrails memo).
CIVIX_EVR_HOST = "goelect.txelections.civixapps.com"

EVR_HTTP_USER_AGENT = (
    "Abstract-Data/texas-turnout-scraper (EV early-vote sync; "
    "+https://github.com/Abstract-Data/texas-turnout-scraper)"
)
EVR_RETRY_STATUS_CODES = frozenset({429, 502, 503, 504})
EVR_HTTP_MAX_RETRIES = 4
EVR_TURNOUT_TIMEOUT_SECONDS = 60.0
EVR_STATEWIDE_TIMEOUT_SECONDS = 180.0
EVR_MIN_REQUEST_INTERVAL_SECONDS = 1.0

_last_evr_request_monotonic = 0.0

EarlyVotePayloadKind = Literal["turnout", "statewide_roster"]

# Cloudflare's published paid-plan cap is 10 GB (decimal), not 10 GiB.
# https://developers.cloudflare.com/d1/platform/limits/
D1_DATABASE_CAP_BYTES = 10_000_000_000

# Peak complete files in one database while a publish is in flight:
# the pointer target, the file that still matches the stored KV hash when
# that is a different file_id (a bump that failed after the pointer flip),
# and the new staged copy. Two copies would miss that window. Every payload
# is base64: 4 stored bytes per 3 raw bytes.
PEAK_STORED_COPIES = 3

# Bound parameters are strings. Stay under the 100 KB SQL statement limit and
# the 2 MB row limit by a wide margin.
# https://developers.cloudflare.com/d1/platform/limits/
CHUNK_RAW_BYTES = 32_768
_INSERT_BATCH = 20

CLOUDFLARE_API_BASE = "https://api.cloudflare.com/client/v4"

KV_NOT_FOR_ELECTION_NIGHT = (
    "KV propagation is about 60 seconds and is not fit for election-night results. "
    "A faster path is not built."
)

_FILE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS early_vote_file (
    source_election_id TEXT NOT NULL,
    file_id TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    byte_length INTEGER NOT NULL,
    chunk_count INTEGER NOT NULL,
    PRIMARY KEY (source_election_id, file_id)
)
""".strip()

_CHUNK_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS early_vote_chunk (
    source_election_id TEXT NOT NULL,
    file_id TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    payload_b64 TEXT NOT NULL,
    PRIMARY KEY (source_election_id, file_id, chunk_index)
)
""".strip()

_POINTER_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS early_vote_pointer (
    source_election_id TEXT PRIMARY KEY,
    file_id TEXT NOT NULL
)
""".strip()

_DAY_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS early_vote_day (
    source_election_id TEXT NOT NULL,
    ev_date TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    file_id TEXT NOT NULL,
    PRIMARY KEY (source_election_id, ev_date)
)
""".strip()

SyncStatus = Literal["unchanged", "dry_run", "wrote"]


class EarlyVoteCapError(RuntimeError):
    """The load would pass the 10 GB D1 per-database cap."""


class EarlyVoteSyncError(RuntimeError):
    """The sync could not finish. KV was not bumped after a failed D1 write."""


@dataclass(frozen=True)
class D1Plan:
    """SQL text a dry run prints. Bound file bytes are omitted."""

    statements: tuple[tuple[str, int], ...]
    chunk_rows: int
    file_rows: int
    source_rows: int | None
    decoded_bytes: int
    cap_bytes: int
    within_cap: bool
    payload_kind: EarlyVotePayloadKind
    csv_columns: tuple[str, ...] | None = None
    dropped_columns: tuple[str, ...] | None = None
    voting_method_counts: tuple[tuple[str, int], ...] | None = None
    aggregate_rows: int | None = None


@dataclass(frozen=True)
class SyncResult:
    """Outcome of one election sync. ``decoded_bytes`` is a count, not the file."""

    election_id: str
    sha256: str
    decoded_bytes: int
    status: SyncStatus
    exit_code: int
    payload_kind: EarlyVotePayloadKind = "turnout"
    version: str | None = None
    plan: D1Plan | None = None


class EarlyVoteStore(Protocol):
    """Hash read, D1 load, and the KV version bump."""

    def stored_hash(self, election_id: str, *, ev_date_iso: str | None = None) -> str | None:
        """Return stored hash for KV (no day) or one EV date row in D1."""

    def write_database(
        self,
        election_id: str,
        decoded: bytes,
        digest: str,
        *,
        ev_date_iso: str | None = None,
        update_pointer: bool = False,
    ) -> None:
        """Stage a new file_id and flip the pointer once every chunk is written."""

    def bump_version(self, election_id: str, digest: str) -> str:
        """Bump ``ev:<election>:version`` and then store ``digest``."""

    def finalize_published_roster(
        self,
        election_id: str,
        ev_date_iso: str,
        content_digest: str,
        *,
        manifest_sha256: str | None = None,
    ) -> str:
        """Point ``early_vote_pointer`` at one day file and bump KV content hash."""


class EmptyHashStore:
    """No Cloudflare calls.

    ``stored`` is an optional hash from an earlier run of the same file, so a
    second dry run can take the unchanged path without reading KV.
    Write methods refuse so a dry run cannot create a database.
    """

    def __init__(
        self,
        stored: str | None = None,
        *,
        day_hashes: dict[tuple[str, str], str] | None = None,
    ) -> None:
        self._stored = stored
        self._day_hashes = day_hashes or {}

    def stored_hash(self, election_id: str, *, ev_date_iso: str | None = None) -> str | None:
        if ev_date_iso:
            return self._day_hashes.get((election_id, ev_date_iso))
        return self._stored

    def write_database(
        self,
        election_id: str,
        decoded: bytes,
        digest: str,
        *,
        ev_date_iso: str | None = None,
        update_pointer: bool = False,
    ) -> None:
        raise EarlyVoteSyncError("refusing to write D1 without --apply")

    def bump_version(self, election_id: str, digest: str) -> str:
        raise EarlyVoteSyncError("refusing to bump KV without --apply")

    def finalize_published_roster(
        self,
        election_id: str,
        ev_date_iso: str,
        content_digest: str,
        *,
        manifest_sha256: str | None = None,
    ) -> str:
        raise EarlyVoteSyncError("refusing to bump KV without --apply")


def database_name(election_id: str) -> str:
    """One D1 name for this election. The id text, including leading zeros, is kept."""
    return f"ev-{require_election_id(election_id)}"


def version_key(election_id: str) -> str:
    """KV key bumped only after a successful D1 write."""
    return f"ev:{require_election_id(election_id)}:version"


def hash_key(election_id: str) -> str:
    """KV key holding the hash of the last file whose version bump succeeded."""
    return f"ev:{require_election_id(election_id)}:sha256"


def manifest_key(election_id: str) -> str:
    """KV key for optional backfill manifest digest (not a file content hash)."""
    return f"ev:{require_election_id(election_id)}:manifest_sha256"


def iso_ev_date(ev_day: date) -> str:
    """Canonical EV date key for per-day D1 rows."""
    return ev_day.isoformat()


def backfill_manifest_digest(day_hashes: dict[str, str]) -> str:
    """Stable manifest hash over sorted per-day content digests."""
    lines = [f"{day}:{digest}" for day, digest in sorted(day_hashes.items())]
    return sha256_decoded("\n".join(lines).encode("utf-8"))


def sha256_decoded(decoded: bytes) -> str:
    """SHA-256 hex digest of decoded EVR bytes (after the base64 unwrap)."""
    return hashlib.sha256(decoded).hexdigest()


def base64_payload_bytes(raw_bytes: int) -> int:
    """Stored size of one payload. Base64 uses 4 bytes for every 3 raw bytes."""
    if raw_bytes < 0:
        raise EarlyVoteCapError("decoded length is negative")
    if raw_bytes == 0:
        return 0
    return 4 * ((raw_bytes + 2) // 3)


def estimated_stored_bytes(raw_bytes: int) -> int:
    """Bytes one publish can hold: retained copies, each stored as base64."""
    return base64_payload_bytes(raw_bytes) * PEAK_STORED_COPIES


def assert_within_cap(byte_length: int, election_id: str) -> None:
    """Refuse a load whose retained base64 copies would pass 10 GB.

    A publish keeps the pointer file and, when it differs, the file that
    still matches the stored KV hash, then stages the new copy. Each copy is
    base64. Equal to the cap is still allowed. Does not create a database
    and does not open a second one.
    """
    stored = estimated_stored_bytes(byte_length)
    cap = D1_DATABASE_CAP_BYTES
    if stored > cap:
        raise EarlyVoteCapError(
            f"early-vote file for election {election_id} is {byte_length} bytes, "
            f"about {stored} stored bytes with {PEAK_STORED_COPIES} retained copies, "
            f"over the {cap} byte D1 cap"
        )


def iter_chunk_b64(decoded: bytes) -> list[str]:
    """Split decoded bytes into base64 chunks that fit a D1 bound parameter."""
    chunks: list[str] = []
    for start in range(0, len(decoded), CHUNK_RAW_BYTES):
        piece = decoded[start : start + CHUNK_RAW_BYTES]
        chunks.append(base64.b64encode(piece).decode("ascii"))
    return chunks


_CHUNK_INSERT_SQL = (
    "INSERT INTO early_vote_chunk "
    "(source_election_id, file_id, chunk_index, payload_b64) VALUES (?, ?, ?, ?)"
)
_FILE_INSERT_SQL = (
    "INSERT INTO early_vote_file "
    "(source_election_id, file_id, sha256, byte_length, chunk_count) "
    "VALUES (?, ?, ?, ?, ?)"
)
_POINTER_UPSERT_SQL = (
    "INSERT INTO early_vote_pointer (source_election_id, file_id) VALUES (?, ?) "
    "ON CONFLICT(source_election_id) DO UPDATE SET file_id = excluded.file_id"
)
_DELETE_CHUNK_SQL = "DELETE FROM early_vote_chunk WHERE source_election_id = ? AND file_id = ?"
_DELETE_FILE_SQL = "DELETE FROM early_vote_file WHERE source_election_id = ? AND file_id = ?"
_POINTER_SELECT_SQL = "SELECT file_id FROM early_vote_pointer WHERE source_election_id = ?"
_FILE_IDS_SQL = (
    "SELECT file_id FROM early_vote_chunk WHERE source_election_id = ? "
    "UNION SELECT file_id FROM early_vote_file WHERE source_election_id = ?"
)
_FILE_ID_FOR_HASH_SQL = (
    "SELECT file_id FROM early_vote_file WHERE source_election_id = ? AND sha256 = ?"
)
_DAY_SELECT_HASH_SQL = (
    "SELECT sha256 FROM early_vote_day WHERE source_election_id = ? AND ev_date = ?"
)
_DAY_SELECT_FILE_SQL = (
    "SELECT file_id FROM early_vote_day WHERE source_election_id = ? AND ev_date = ?"
)
_DAY_UPSERT_SQL = (
    "INSERT INTO early_vote_day (source_election_id, ev_date, sha256, file_id) "
    "VALUES (?, ?, ?, ?) ON CONFLICT(source_election_id, ev_date) DO UPDATE SET "
    "sha256 = excluded.sha256, file_id = excluded.file_id"
)
_DAY_FILE_IDS_SQL = "SELECT file_id FROM early_vote_day WHERE source_election_id = ?"
_DAY_SELECT_FILE_AND_HASH_SQL = (
    "SELECT file_id, sha256 FROM early_vote_day WHERE source_election_id = ? AND ev_date = ?"
)
_FILE_CHUNK_COUNT_SQL = (
    "SELECT chunk_count FROM early_vote_file WHERE source_election_id = ? AND file_id = ?"
)
_CHUNK_PAYLOAD_SQL = (
    "SELECT payload_b64 FROM early_vote_chunk "
    "WHERE source_election_id = ? AND file_id = ? AND chunk_index = ?"
)
_SCHEMA_CREATES: tuple[str, ...] = (
    _CHUNK_TABLE_SQL,
    _FILE_TABLE_SQL,
    _POINTER_TABLE_SQL,
    _DAY_TABLE_SQL,
    DAY_CHANGE_TABLE_SQL,
    *PRECOMPUTE_SCHEMA_CREATES,
)
_CHUNK_SCHEMA_SQL = "SELECT name FROM pragma_table_info('early_vote_chunk')"
_FILE_SCHEMA_SQL = "SELECT name FROM pragma_table_info('early_vote_file')"

Statement = tuple[str, tuple[str, ...]]


@dataclass(frozen=True)
class PublishPlan:
    """Staged statements for one new file.

    ``pointer_update`` is a single statement and comes after every chunk insert.
    Stale deletes are not bound here. Apply reads them from D1 before this
    file is inserted, and this ``file_id`` is not one of them.
    """

    file_id: str
    creates: tuple[Statement, ...]
    chunk_inserts: tuple[Statement, ...]
    file_insert: Statement
    pointer_update: Statement


@dataclass(frozen=True)
class EarlyVoteFileView:
    """Reader metadata for one file_id. Counts only — never chunk payloads."""

    file_id: str
    sha256: str
    chunk_count: int
    chunks_present: int

    @property
    def complete(self) -> bool:
        """True when ``chunk_count`` equals the chunk rows stored for this file."""
        return self.chunk_count >= 0 and self.chunk_count == self.chunks_present


def new_file_id(digest: str) -> str:
    """String id for one load attempt. The election id is not reused as this id."""
    return f"{digest}-{uuid.uuid4().hex}"


def build_publish_plan(election_id: str, decoded: bytes, digest: str, file_id: str) -> PublishPlan:
    """Chunk inserts, then the file row, then one pointer statement."""
    election_id = require_election_id(election_id)
    if not isinstance(file_id, str) or not file_id:
        raise EarlyVoteSyncError("file_id must be a non-empty string")
    chunks = iter_chunk_b64(decoded)
    chunk_inserts = tuple(
        (_CHUNK_INSERT_SQL, (election_id, file_id, str(index), payload))
        for index, payload in enumerate(chunks)
    )
    return PublishPlan(
        file_id=file_id,
        creates=(
            (_CHUNK_TABLE_SQL, ()),
            (_FILE_TABLE_SQL, ()),
            (_POINTER_TABLE_SQL, ()),
            (_DAY_TABLE_SQL, ()),
        ),
        chunk_inserts=chunk_inserts,
        file_insert=(
            _FILE_INSERT_SQL,
            (election_id, file_id, digest, str(len(decoded)), str(len(chunks))),
        ),
        pointer_update=(_POINTER_UPSERT_SQL, (election_id, file_id)),
    )


def choose_published_file(
    *,
    pointer_file_id: str | None,
    files: Sequence[EarlyVoteFileView],
    kv_sha256: str | None,
) -> EarlyVoteFileView | None:
    # Worker and read client: call this before any ``early_vote_chunk`` read.
    # Trust a file only when early_vote_file.chunk_count equals the number of
    # chunk rows for that file_id and sha256 equals the KV hash written for
    # the bumped ev:<election>:version. Load chunks only for the file_id on
    # the returned view. None means the load is incomplete — do not serve a
    # partial chunk set. A stage that has not flipped early_vote_pointer stays
    # invisible, so an interrupted load leaves readers on the previous complete
    # file. If the pointer already names the new file and KV still has the
    # previous hash, this still returns that previous complete file.
    if kv_sha256 is None or kv_sha256 == "":
        return None
    pointer = _file_by_id(files, pointer_file_id)
    if pointer is not None and pointer.complete and pointer.sha256 == kv_sha256:
        return pointer
    return _complete_with_hash(files, kv_sha256)


def _file_by_id(
    files: Sequence[EarlyVoteFileView], file_id: str | None
) -> EarlyVoteFileView | None:
    if file_id is None:
        return None
    for item in files:
        if item.file_id == file_id:
            return item
    return None


def _complete_with_hash(
    files: Sequence[EarlyVoteFileView], sha256: str
) -> EarlyVoteFileView | None:
    for item in files:
        if item.complete and item.sha256 == sha256:
            return item
    return None


def source_row_count(decoded: bytes) -> int | None:
    """Count ``turnout_by_county`` rows. Other shapes stay uncounted.

    A parse error is ignored. Its message can quote the file, so it is not logged.
    """
    try:
        payload = json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    rows = payload.get("turnout_by_county")
    if isinstance(rows, list):
        return len(rows)
    return None


def stale_file_ids(
    present: Sequence[object],
    *,
    pointer_file_id: str | None,
    kv_hash_file_ids: Sequence[str],
    day_file_ids: Sequence[str] = (),
) -> tuple[str, ...]:
    """File ids to drop. Keep pointer, KV-hash matches, and per-day backfill files."""
    keep: set[str] = set()
    if isinstance(pointer_file_id, str) and pointer_file_id:
        keep.add(pointer_file_id)
    for file_id in kv_hash_file_ids:
        if file_id:
            keep.add(file_id)
    for file_id in day_file_ids:
        if file_id:
            keep.add(file_id)
    seen: set[str] = set()
    stale: list[str] = []
    for file_id in present:
        if not isinstance(file_id, str) or not file_id or file_id in seen:
            continue
        seen.add(file_id)
        if file_id not in keep:
            stale.append(file_id)
    return tuple(stale)


def _plan_statements(plan: PublishPlan) -> tuple[Statement, ...]:
    """Creates, then stale deletes from the previous run, then the new file.

    Delete parameters are bound at apply time from D1. The file being
    published is not in that set, so these statements do not name it.
    """
    return (
        *plan.creates,
        (_DELETE_CHUNK_SQL, ()),
        (_DELETE_FILE_SQL, ()),
        *plan.chunk_inserts,
        plan.file_insert,
        plan.pointer_update,
    )


def prepare_payload_bytes(
    decoded: bytes,
    payload_kind: EarlyVotePayloadKind,
) -> tuple[bytes, RosterCsvSummary | None]:
    """Decode envelope bytes are prepared for hash + D1 (roster redaction when needed)."""
    if payload_kind == "turnout":
        return decoded, None
    redacted, summary = redact_roster_csv(decoded, statewide=True)
    return redacted, summary


def summarize_d1_plan(
    election_id: str,
    decoded: bytes,
    digest: str,
    *,
    payload_kind: EarlyVotePayloadKind = "turnout",
    roster_summary: RosterCsvSummary | None = None,
) -> D1Plan:
    """Statement text and row counts for a dry run. Bound parameters are dropped.

    Deletes are listed before the new inserts. Apply binds ``file_id`` from
    D1 at the start of the write and skips the pointer target and the KV hash.
    """
    election_id = require_election_id(election_id)
    plan = build_publish_plan(election_id, decoded, digest, new_file_id(digest))
    statements = _plan_statements(plan)
    counts: dict[str, int] = {}
    order: list[str] = []
    chunk_rows = 0
    file_rows = 0
    for sql, _params in statements:
        collapsed = " ".join(sql.split())
        if collapsed not in counts:
            order.append(collapsed)
            counts[collapsed] = 0
        counts[collapsed] += 1
        if sql == _CHUNK_INSERT_SQL:
            chunk_rows += 1
        elif sql == _FILE_INSERT_SQL:
            file_rows += 1
    if roster_summary is not None:
        source_rows = roster_summary.est_rows
    else:
        source_rows = source_row_count(decoded)
    aggregate_rows: int | None = None
    try:
        agg_body = aggregate_body_from_decoded(decoded, source_election_id=election_id)
        aggregate_rows = precompute_row_count(agg_body)
    except ValueError:
        aggregate_rows = None
    return D1Plan(
        statements=tuple((sql, counts[sql]) for sql in order),
        chunk_rows=chunk_rows,
        file_rows=file_rows,
        source_rows=source_rows,
        decoded_bytes=len(decoded),
        cap_bytes=D1_DATABASE_CAP_BYTES,
        within_cap=estimated_stored_bytes(len(decoded)) <= D1_DATABASE_CAP_BYTES,
        payload_kind=payload_kind,
        csv_columns=roster_summary.columns if roster_summary else None,
        dropped_columns=roster_summary.dropped_columns if roster_summary else None,
        voting_method_counts=roster_summary.voting_method_counts if roster_summary else None,
        aggregate_rows=aggregate_rows,
    )


def sync_early_vote(
    election_id: str,
    response: Any,
    store: EarlyVoteStore,
    *,
    dry_run: bool = False,
    payload_kind: EarlyVotePayloadKind = "turnout",
    ev_date_iso: str | None = None,
    defer_kv_bump: bool = False,
) -> SyncResult:
    """Hash the unwrapped file and write D1, then KV, only when the hash is new.

    ``response`` is a Civix HTTP response (``.text`` and ``.json()``). The hash
    covers decoded bytes after roster redaction (when ``payload_kind`` is
    ``statewide_roster``), not the JSON envelope text.
    """
    election_id = require_election_id(election_id)
    raw = _decode_envelope(response)
    decoded, roster_summary = prepare_payload_bytes(raw, payload_kind)
    digest = sha256_decoded(decoded)
    prior = store.stored_hash(election_id, ev_date_iso=ev_date_iso)
    if prior == digest:
        logger.info(
            "early-vote unchanged election_id=%s ev_date=%s sha256=%s decoded_bytes=%s",
            election_id,
            ev_date_iso or "",
            digest,
            len(decoded),
        )
        return SyncResult(
            election_id=election_id,
            sha256=digest,
            decoded_bytes=len(decoded),
            status="unchanged",
            exit_code=0,
            payload_kind=payload_kind,
        )

    assert_within_cap(len(decoded), election_id)
    if dry_run:
        plan = summarize_d1_plan(
            election_id,
            decoded,
            digest,
            payload_kind=payload_kind,
            roster_summary=roster_summary,
        )
        logger.info(
            "early-vote dry-run election_id=%s sha256=%s decoded_bytes=%s d1_rows=%s within_cap=%s",
            election_id,
            digest,
            len(decoded),
            plan.chunk_rows + plan.file_rows,
            plan.within_cap,
        )
        return SyncResult(
            election_id=election_id,
            sha256=digest,
            decoded_bytes=len(decoded),
            status="dry_run",
            exit_code=0,
            payload_kind=payload_kind,
            plan=plan,
        )

    update_pointer = bool(ev_date_iso) and not defer_kv_bump
    store.write_database(
        election_id,
        decoded,
        digest,
        ev_date_iso=ev_date_iso,
        update_pointer=update_pointer,
    )
    version = None
    if not defer_kv_bump:
        version = store.bump_version(election_id, digest)
    logger.info(
        "early-vote wrote election_id=%s ev_date=%s sha256=%s decoded_bytes=%s version=%s",
        election_id,
        ev_date_iso or "",
        digest,
        len(decoded),
        version,
    )
    return SyncResult(
        election_id=election_id,
        sha256=digest,
        decoded_bytes=len(decoded),
        status="wrote",
        exit_code=0,
        payload_kind=payload_kind,
        version=version,
    )


def format_sync_report(result: SyncResult) -> str:
    """Operator report. Counts and ids only — never the file body."""
    lines = [
        f"election_id={result.election_id}",
        f"payload_kind={result.payload_kind}",
        f"sha256={result.sha256}",
        f"decoded_bytes={result.decoded_bytes}",
        f"status={result.status}",
        f"exit_code={result.exit_code}",
        f"d1_database={database_name(result.election_id)}",
        f"kv_version_key={version_key(result.election_id)}",
        f"kv_hash_key={hash_key(result.election_id)}",
    ]
    if result.version is not None:
        lines.append(f"kv_version={result.version}")
    if result.status == "unchanged":
        lines.append("unchanged_skip=true")
    if result.plan is not None:
        plan = result.plan
        lines.append(f"d1_cap_bytes={plan.cap_bytes}")
        lines.append(f"within_cap={'true' if plan.within_cap else 'false'}")
        row_fields = [f"election_id={result.election_id}"]
        if plan.csv_columns:
            row_fields.append(f"csv_columns={','.join(plan.csv_columns)}")
        if plan.dropped_columns:
            row_fields.append(f"dropped_columns={','.join(plan.dropped_columns)}")
        if plan.voting_method_counts:
            rollups = ",".join(f"{name}:{count}" for name, count in plan.voting_method_counts)
            row_fields.append(f"voting_method_counts={rollups}")
        if plan.source_rows is not None:
            row_fields.append(f"source_rows={plan.source_rows}")
        if plan.aggregate_rows is not None:
            row_fields.append(f"d1_aggregate_rows={plan.aggregate_rows}")
        row_fields.append(f"d1_chunk_rows={plan.chunk_rows}")
        row_fields.append(f"d1_file_rows={plan.file_rows}")
        row_fields.append(f"d1_rows={plan.chunk_rows + plan.file_rows}")
        lines.append("rows " + " ".join(row_fields))
        for sql, repeats in plan.statements:
            lines.append(f"d1_statement repeats={repeats} sql={sql}")
    lines.append(f"note={KV_NOT_FOR_ELECTION_NIGHT}")
    return "\n".join(lines)


def parse_mdy(value: str) -> date:
    """Parse ``MM/DD/YYYY`` without ``strptime`` (no timezone on a calendar date)."""
    parts = value.split("/")
    if len(parts) != 3:
        raise ValueError("election date must be MM/DD/YYYY")
    month_text, day_text, year_text = parts
    if not (month_text.isdigit() and day_text.isdigit() and year_text.isdigit()):
        raise ValueError("election date must be MM/DD/YYYY")
    try:
        return date(int(year_text), int(month_text), int(day_text))
    except ValueError as exc:
        raise ValueError("election date must be MM/DD/YYYY") from exc


def _assert_civix_host() -> None:
    if CIVIX_EVR_HOST not in BASE_URL:
        raise EarlyVoteSyncError(f"Civix base URL must use host {CIVIX_EVR_HOST}")


def _evr_throttle() -> None:
    """≥1.0 s between Civix EVR requests (polite scrape policy)."""
    global _last_evr_request_monotonic
    now = time.monotonic()
    wait = EVR_MIN_REQUEST_INTERVAL_SECONDS - (now - _last_evr_request_monotonic)
    if wait > 0:
        time.sleep(wait)
    _last_evr_request_monotonic = time.monotonic()


def _evr_paced_client(
    http_backend: HttpBackend,
    *,
    timeout: float,
) -> PacedHttpClient:
    _assert_civix_host()
    return PacedHttpClient(
        BASE_URL,
        backend=http_backend,
        timeout=timeout,
        headers={"User-Agent": EVR_HTTP_USER_AGENT},
    )


def _evr_get(
    path: str,
    params: dict[str, str],
    *,
    http_backend: HttpBackend,
    timeout: float,
    client: httpx.Client | None = None,
) -> Any:
    """Polite Civix GET with Abstract Data User-Agent and 429/503 backoff."""
    _evr_throttle()
    if client is not None:
        response = client.get(f"{BASE_URL}{path}", params=params)
        response.raise_for_status()
        return response
    paced = _evr_paced_client(http_backend, timeout=timeout)
    try:
        return paced.get(
            path,
            params=params,
            retry_status_codes=EVR_RETRY_STATUS_CODES,
            max_retries=EVR_HTTP_MAX_RETRIES,
        )
    finally:
        paced.close()


def pull_early_vote_file(
    election_id: str,
    election_date: date,
    *,
    client: httpx.Client | None = None,
    http_backend: HttpBackend = "httpx",
) -> Any:
    """GET the Civix early-vote turnout JSON. ``electionId`` is sent as a string.

    Endpoint (docs/CIVIX_EVR_API.md)::

        GET /api-ivis-system/api/v1/getFile
            ?type=EVR_EARLYVOTING&electionId={id}&electionDate={MM/DD/YYYY}
    """
    election_id = require_election_id(election_id)
    params = {
        "type": "EVR_EARLYVOTING",
        "electionId": election_id,
        "electionDate": election_date.strftime("%m/%d/%Y"),
    }
    path = f"{API_PREFIX}/getFile"
    return _evr_get(
        path,
        params,
        http_backend=http_backend,
        timeout=EVR_TURNOUT_TIMEOUT_SECONDS,
        client=client,
    )


def pull_statewide_roster_file(
    election_id: str,
    election_date: date,
    *,
    client: httpx.Client | None = None,
    http_backend: HttpBackend = "httpx",
) -> Any:
    """GET the Civix statewide EV roster CSV (``EVR_STATEWIDE``).

    Endpoint::

        GET /api-ivis-system/api/v1/getFile
            ?type=EVR_STATEWIDE&electionId={id}&electionDate={MM/DD/YYYY}
    """
    election_id = require_election_id(election_id)
    params = {
        "type": "EVR_STATEWIDE",
        "electionId": election_id,
        "electionDate": election_date.strftime("%m/%d/%Y"),
    }
    path = f"{API_PREFIX}/getFile"
    return _evr_get(
        path,
        params,
        http_backend=http_backend,
        timeout=EVR_STATEWIDE_TIMEOUT_SECONDS,
        client=client,
    )


def _cloudflare_config_from_env() -> tuple[str, str, str] | None:
    account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "").strip()
    api_token = os.environ.get("CLOUDFLARE_API_TOKEN", "").strip()
    namespace_id = os.environ.get("CLOUDFLARE_KV_NAMESPACE_ID", "").strip()
    if account_id and api_token and namespace_id:
        return account_id, api_token, namespace_id
    return None


class CloudflareEarlyVoteStore:
    """D1 and KV via the Cloudflare REST API. Account id comes from the caller."""

    def __init__(
        self,
        *,
        account_id: str,
        api_token: str,
        kv_namespace_id: str,
        client: httpx.Client | None = None,
        snapshots: R2SnapshotStore | None = None,
    ) -> None:
        if not account_id or not api_token or not kv_namespace_id:
            raise EarlyVoteSyncError(
                "Cloudflare account id, API token, and KV namespace id are required"
            )
        self._account_id = account_id
        self._namespace_id = kv_namespace_id
        self._snapshots = snapshots if snapshots is not None else snapshot_store_from_env()
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=CLOUDFLARE_API_BASE,
            headers={"Authorization": f"Bearer {api_token}"},
            timeout=60.0,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def stored_hash(self, election_id: str, *, ev_date_iso: str | None = None) -> str | None:
        if ev_date_iso:
            election_id = require_election_id(election_id)
            database_id = self._database_uuid_if_exists(election_id)
            if database_id is None:
                return None
            self._ensure_schema(database_id)
            rows = self._query_rows(
                database_id,
                _DAY_SELECT_HASH_SQL,
                [election_id, ev_date_iso],
            )
            if not rows:
                return None
            digest = rows[0].get("sha256")
            return digest if isinstance(digest, str) and digest else None
        value = self._kv_get(hash_key(election_id))
        if value is None:
            return None
        text = value.strip()
        return text or None

    def write_database(
        self,
        election_id: str,
        decoded: bytes,
        digest: str,
        *,
        ev_date_iso: str | None = None,
        update_pointer: bool = False,
    ) -> None:
        election_id = require_election_id(election_id)
        assert_within_cap(len(decoded), election_id)
        database_id = self._ensure_one_database(election_id)
        self._reject_legacy_schema(database_id)
        self._ensure_schema(database_id)
        plan = build_publish_plan(election_id, decoded, digest, new_file_id(digest))
        if ev_date_iso:
            self._write_day_scoped(
                database_id,
                election_id,
                ev_date_iso,
                plan,
                digest,
                decoded,
                update_pointer=update_pointer,
            )
            return
        # Previous complete files stay until a later run. This pass only drops
        # ids that are already neither the pointer nor the stored KV hash.
        self._delete_stale_files(database_id, election_id)
        self._insert_chunks(database_id, plan.chunk_inserts)
        self._statements(database_id, [_statement_lists(plan.file_insert)])
        self._write_precomputed_aggregates(
            database_id,
            election_id,
            plan.file_id,
            decoded,
            ev_date_iso=None,
        )
        # One statement, after every chunk. A failed batch before this leaves
        # the previous pointer in place.
        self._statements(database_id, [_statement_lists(plan.pointer_update)])
        logger.info(
            "early-vote D1 pointer swap election_id=%s database=%s chunks=%s decoded_bytes=%s",
            election_id,
            database_name(election_id),
            len(plan.chunk_inserts),
            len(decoded),
        )

    def _write_day_scoped(
        self,
        database_id: str,
        election_id: str,
        ev_date_iso: str,
        plan: PublishPlan,
        digest: str,
        decoded: bytes,
        *,
        update_pointer: bool = False,
    ) -> None:
        rows = self._query_rows(
            database_id,
            _DAY_SELECT_FILE_AND_HASH_SQL,
            [election_id, ev_date_iso],
        )
        old_id: str | None = None
        old_sha: str | None = None
        if rows:
            candidate = rows[0].get("file_id")
            if isinstance(candidate, str) and candidate:
                old_id = candidate
            candidate_sha = rows[0].get("sha256")
            if isinstance(candidate_sha, str) and candidate_sha:
                old_sha = candidate_sha
        if old_id and old_id != plan.file_id and old_sha and old_sha != digest:
            self._keep_replaced_version(
                database_id, election_id, ev_date_iso, old_id, old_sha, digest, decoded
            )
        elif self._snapshots is not None:
            self._snapshots.record_version(
                election_id,
                ev_date_iso,
                sha256=digest,
                decoded=decoded,
                rows=roster_row_count(decoded),
            )
        self._insert_chunks(database_id, plan.chunk_inserts)
        self._statements(database_id, [_statement_lists(plan.file_insert)])
        self._write_precomputed_aggregates(
            database_id,
            election_id,
            plan.file_id,
            decoded,
            ev_date_iso=ev_date_iso,
        )
        self._statements(
            database_id,
            [
                (
                    _DAY_UPSERT_SQL,
                    [election_id, ev_date_iso, digest, plan.file_id],
                )
            ],
        )
        if update_pointer:
            self._statements(
                database_id,
                [_statement_lists(plan.pointer_update)],
            )
        if old_id and old_id != plan.file_id:
            repoint: list[tuple[str, list[str]]] = []
            if not update_pointer and self._pointer_file_id(database_id, election_id) == old_id:
                repoint.append(_statement_lists(plan.pointer_update))
            self._statements(
                database_id,
                [
                    *repoint,
                    *[
                        _statement_lists(item)
                        for item in delete_precompute_statements(election_id, old_id)
                    ],
                    (_DELETE_CHUNK_SQL, [election_id, old_id]),
                    (_DELETE_FILE_SQL, [election_id, old_id]),
                ],
            )
        logger.info(
            "early-vote D1 day upsert election_id=%s ev_date=%s database=%s "
            "chunks=%s decoded_bytes=%s",
            election_id,
            ev_date_iso,
            database_name(election_id),
            len(plan.chunk_inserts),
            len(decoded),
        )

    def _keep_replaced_version(
        self,
        database_id: str,
        election_id: str,
        ev_date_iso: str,
        old_id: str,
        old_sha: str,
        new_sha: str,
        decoded: bytes,
    ) -> None:
        """Snapshot both versions to R2, then log voters added and removed in D1.

        Runs before the old file is deleted, so a failure here leaves D1 as it was.
        """
        old_bytes = self._load_file_bytes(database_id, election_id, old_id)
        if self._snapshots is not None:
            self._snapshots.record_version(
                election_id,
                ev_date_iso,
                sha256=old_sha,
                decoded=old_bytes,
                rows=roster_row_count(old_bytes),
            )
            self._snapshots.record_version(
                election_id,
                ev_date_iso,
                sha256=new_sha,
                decoded=decoded,
                rows=roster_row_count(decoded),
            )
        diff = diff_rosters(old_bytes, decoded)
        if diff is None:
            return
        statements = change_statements(
            election_id,
            ev_date_iso,
            old_sha256=old_sha,
            new_sha256=new_sha,
            diff=diff,
            recorded_at=datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        for start in range(0, len(statements), _INSERT_BATCH):
            self._statements(database_id, statements[start : start + _INSERT_BATCH])
        logger.info(
            "early-vote day change logged election_id=%s ev_date=%s added=%s removed=%s",
            election_id,
            ev_date_iso,
            len(diff.added),
            len(diff.removed),
        )

    def _load_file_bytes(self, database_id: str, election_id: str, file_id: str) -> bytes:
        rows = self._query_rows(database_id, _FILE_CHUNK_COUNT_SQL, [election_id, file_id])
        if not rows:
            raise EarlyVoteSyncError("stored day file has no file row")
        count = int(rows[0]["chunk_count"])
        parts: list[bytes] = []
        for index in range(count):
            chunk = self._query_rows(
                database_id, _CHUNK_PAYLOAD_SQL, [election_id, file_id, str(index)]
            )
            if not chunk:
                raise EarlyVoteSyncError("stored day file is missing a chunk")
            parts.append(base64.b64decode(str(chunk[0]["payload_b64"])))
        return b"".join(parts)

    def _delete_stale_files(self, database_id: str, election_id: str) -> None:
        present = self._distinct_file_ids(database_id, election_id)
        pointer = self._pointer_file_id(database_id, election_id)
        digest = self.stored_hash(election_id)
        hash_ids: tuple[str, ...] = ()
        if digest is not None:
            hash_ids = self._file_ids_for_hash(database_id, election_id, digest)
        day_ids = self._day_scoped_file_ids(database_id, election_id)
        stale = stale_file_ids(
            present,
            pointer_file_id=pointer,
            kv_hash_file_ids=hash_ids,
            day_file_ids=day_ids,
        )
        if not stale:
            return
        rows: list[tuple[str, list[str]]] = []
        for file_id in stale:
            for stmt in delete_precompute_statements(election_id, file_id):
                rows.append(_statement_lists(stmt))
            rows.append((_DELETE_CHUNK_SQL, [election_id, file_id]))
            rows.append((_DELETE_FILE_SQL, [election_id, file_id]))
        for start in range(0, len(rows), _INSERT_BATCH):
            self._statements(database_id, rows[start : start + _INSERT_BATCH])
        logger.info(
            "early-vote D1 dropped stale files election_id=%s dropped=%s",
            election_id,
            len(stale),
        )

    def _distinct_file_ids(self, database_id: str, election_id: str) -> list[str]:
        rows = self._query_rows(database_id, _FILE_IDS_SQL, [election_id, election_id])
        return _string_column(rows, "file_id")

    def _file_ids_for_hash(
        self, database_id: str, election_id: str, digest: str
    ) -> tuple[str, ...]:
        rows = self._query_rows(database_id, _FILE_ID_FOR_HASH_SQL, [election_id, digest])
        return tuple(_string_column(rows, "file_id"))

    def bump_version(self, election_id: str, digest: str) -> str:
        """Write the version key, then the file content hash key."""
        key = version_key(election_id)
        current = self._kv_get(key)
        nxt = _next_version(current)
        self._kv_put(key, nxt)
        self._kv_put(hash_key(election_id), digest)
        return nxt

    def finalize_published_roster(
        self,
        election_id: str,
        ev_date_iso: str,
        content_digest: str,
        *,
        manifest_sha256: str | None = None,
    ) -> str:
        """Flip pointer to one day's complete file, then bump KV with its content hash."""
        election_id = require_election_id(election_id)
        database_id = self._database_uuid_if_exists(election_id)
        if database_id is None:
            raise EarlyVoteSyncError(
                f"election_id {election_id}: cannot publish roster before D1 database exists"
            )
        rows = self._query_rows(
            database_id,
            _DAY_SELECT_FILE_SQL,
            [election_id, ev_date_iso],
        )
        if not rows:
            raise EarlyVoteSyncError(
                f"election_id {election_id}: no early_vote_day row for ev_date={ev_date_iso}"
            )
        file_id = rows[0].get("file_id")
        if not isinstance(file_id, str) or not file_id:
            raise EarlyVoteSyncError("early_vote_day file_id is not a string")
        self._statements(
            database_id,
            [(_POINTER_UPSERT_SQL, [election_id, file_id])],
        )
        self.backfill_precomputed_aggregates(election_id, dry_run=False)
        from texas_turnout_scraper.early_vote_d1_precompute_backfill import (
            published_precompute_state,
            unreadable_state_error,
        )

        if (
            published_precompute_state(self, election_id, published_digest=content_digest)
            == "unknown"
        ):
            raise unreadable_state_error(election_id)
        version = self.bump_version(election_id, content_digest)
        if manifest_sha256:
            self._kv_put(manifest_key(election_id), manifest_sha256)
        return version

    def _database_uuid_if_exists(self, election_id: str) -> str | None:
        name = database_name(election_id)
        matches = self._list_exact_names(name)
        if len(matches) > 1:
            raise EarlyVoteSyncError(
                f"election {election_id} matched {len(matches)} D1 databases "
                "named for that election; refusing to read"
            )
        if len(matches) == 0:
            return None
        uuid = matches[0].get("uuid")
        if not isinstance(uuid, str) or not uuid:
            raise EarlyVoteSyncError("D1 database is missing a uuid")
        return uuid

    def _ensure_schema(self, database_id: str) -> None:
        self._statements(
            database_id,
            [(sql, []) for sql in _SCHEMA_CREATES],
        )

    def _day_scoped_file_ids(self, database_id: str, election_id: str) -> tuple[str, ...]:
        rows = self._query_rows(database_id, _DAY_FILE_IDS_SQL, [election_id])
        return tuple(_string_column(rows, "file_id"))

    def _ensure_one_database(self, election_id: str) -> str:
        name = database_name(election_id)
        matches = self._list_exact_names(name)
        if len(matches) > 1:
            raise EarlyVoteSyncError(
                f"election {election_id} matched {len(matches)} D1 databases "
                "named for that election; refusing to create another"
            )
        if len(matches) == 1:
            uuid = matches[0].get("uuid")
            if not isinstance(uuid, str) or not uuid:
                raise EarlyVoteSyncError("D1 database is missing a uuid")
            return uuid
        created = self._api(
            "POST", f"/accounts/{self._account_id}/d1/database", json={"name": name}
        )
        return _created_uuid(created)

    def _list_exact_names(self, name: str) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        page = 1
        while page <= 100:
            payload = self._api(
                "GET",
                f"/accounts/{self._account_id}/d1/database",
                params={"name": name, "page": page, "per_page": 100},
            )
            rows, total = _database_page(payload)
            found.extend(row for row in rows if row.get("name") == name)
            if total is not None and page * 100 >= total:
                break
            if len(rows) < 100:
                break
            page += 1
        return found

    def _reject_legacy_schema(self, database_id: str) -> None:
        """Fail closed when a database still has the pre-swap primary key.

        ``CREATE TABLE IF NOT EXISTS`` will not add ``file_id``. Replacing
        rows in that table would delete the published file before the new
        chunks exist.
        """
        for table, sql in (
            ("early_vote_chunk", _CHUNK_SCHEMA_SQL),
            ("early_vote_file", _FILE_SCHEMA_SQL),
        ):
            names = self._column_names(database_id, sql)
            if names and "file_id" not in names:
                raise EarlyVoteSyncError(
                    f"{table} has no file_id column; refusing to replace rows in place"
                )

    def _column_names(self, database_id: str, sql: str) -> list[str]:
        names: list[str] = []
        for row in self._query_rows(database_id, sql, []):
            name = row.get("name")
            if isinstance(name, str):
                names.append(name)
        return names

    def _pointer_file_id(self, database_id: str, election_id: str) -> str | None:
        rows = self._query_rows(database_id, _POINTER_SELECT_SQL, [election_id])
        if not rows:
            return None
        file_id = rows[0].get("file_id")
        if file_id is None:
            return None
        if not isinstance(file_id, str) or not file_id:
            raise EarlyVoteSyncError("early_vote_pointer file_id is not a string")
        return file_id

    def _insert_chunks(self, database_id: str, inserts: tuple[Statement, ...]) -> None:
        rows = [_statement_lists(item) for item in inserts]
        for start in range(0, len(rows), _INSERT_BATCH):
            self._statements(database_id, rows[start : start + _INSERT_BATCH])

    def backfill_precomputed_aggregates(
        self,
        election_id: str,
        *,
        dry_run: bool,
    ) -> PrecomputeBackfillResult:
        from texas_turnout_scraper.early_vote_d1_precompute_backfill import (
            run_precompute_backfill_on_store,
        )

        return run_precompute_backfill_on_store(self, election_id, dry_run=dry_run)

    def _write_precomputed_aggregates(
        self,
        database_id: str,
        election_id: str,
        file_id: str,
        decoded: bytes,
        *,
        ev_date_iso: str | None,
    ) -> None:
        body = aggregate_body_from_decoded(
            decoded,
            source_election_id=election_id,
            ev_date_iso=ev_date_iso,
        )
        statements = [
            _statement_lists(item)
            for item in precompute_statements(
                election_id,
                file_id,
                body,
                ev_date_iso=ev_date_iso,
            )
        ]
        self._statements(database_id, statements)
        logger.info(
            "early-vote D1 precomputed aggregates election_id=%s file_id=%s rows=%s",
            election_id,
            file_id,
            precompute_row_count(body),
        )

    def _record_aggregate_meta(
        self,
        database_id: str,
        election_id: str,
        file_id: str,
        county_count: int,
    ) -> None:
        self._statements(
            database_id,
            [_statement_lists(meta_upsert_statement(election_id, file_id, county_count))],
        )

    def _query_rows(self, database_id: str, sql: str, params: list[str]) -> list[dict[str, Any]]:
        payload = self._api(
            "POST",
            f"/accounts/{self._account_id}/d1/database/{database_id}/query",
            json={"batch": [{"sql": sql, "params": params}]},
        )
        _require_query_success(payload)
        return _first_result_rows(payload)

    def _statements(self, database_id: str, statements: list[tuple[str, list[str]]]) -> None:
        if not statements:
            return
        body = {
            "batch": [{"sql": sql, "params": params} for sql, params in statements],
        }
        payload = self._api(
            "POST",
            f"/accounts/{self._account_id}/d1/database/{database_id}/query",
            json=body,
        )
        _require_query_success(payload)

    def _kv_get(self, key: str) -> str | None:
        path = _kv_value_path(self._account_id, self._namespace_id, key)
        response = self._client.get(path)
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise EarlyVoteSyncError(f"Cloudflare KV read failed with HTTP {response.status_code}")
        content_type = response.headers.get("content-type", "")
        if "application/json" in content_type:
            payload = response.json()
            if isinstance(payload, dict) and payload.get("success") is False:
                raise EarlyVoteSyncError("Cloudflare KV read failed")
        return response.text

    def _kv_put(self, key: str, value: str) -> None:
        path = _kv_value_path(self._account_id, self._namespace_id, key)
        response = self._client.put(
            path,
            content=value.encode("utf-8"),
            headers={"Content-Type": "application/octet-stream"},
        )
        if response.status_code >= 400:
            raise EarlyVoteSyncError(f"Cloudflare KV write failed with HTTP {response.status_code}")
        if response.content:
            payload = response.json()
            if isinstance(payload, dict) and payload.get("success") is False:
                raise EarlyVoteSyncError("Cloudflare KV write failed")

    def _api(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self._client.request(method, path, **kwargs)
        if response.status_code >= 400:
            raise EarlyVoteSyncError(f"Cloudflare HTTP {response.status_code} for {method} {path}")
        payload = response.json()
        if isinstance(payload, dict) and payload.get("success") is False:
            raise EarlyVoteSyncError(f"Cloudflare API error for {method}")
        return payload


def _kv_value_path(account_id: str, namespace_id: str, key: str) -> str:
    """Percent-encode the key so colons survive the KV value URL."""
    encoded = quote(key, safe="")
    return f"/accounts/{account_id}/storage/kv/namespaces/{namespace_id}/values/{encoded}"


def _next_version(current: str | None) -> str:
    if current is None or not current.strip():
        return "1"
    text = current.strip()
    if not text.isdigit():
        raise EarlyVoteSyncError("stored KV version is not a decimal string")
    return str(int(text) + 1)


def _created_uuid(payload: Any) -> str:
    result = payload.get("result") if isinstance(payload, dict) else None
    uuid = result.get("uuid") if isinstance(result, dict) else None
    if not isinstance(uuid, str) or not uuid:
        raise EarlyVoteSyncError("D1 create did not return a database uuid")
    return uuid


def _database_page(payload: Any) -> tuple[list[dict[str, Any]], int | None]:
    if not isinstance(payload, dict):
        raise EarlyVoteSyncError("D1 list response was not a JSON object")
    rows = payload.get("result")
    if not isinstance(rows, list):
        raise EarlyVoteSyncError("D1 list response did not include a result list")
    parsed = [row for row in rows if isinstance(row, dict)]
    info = payload.get("result_info")
    total: int | None = None
    if isinstance(info, dict) and isinstance(info.get("total_count"), int):
        total = info["total_count"]
    return parsed, total


def _statement_lists(statement: Statement) -> tuple[str, list[str]]:
    sql, params = statement
    return sql, list(params)


def _string_column(rows: Sequence[dict[str, Any]], column: str) -> list[str]:
    found: list[str] = []
    for row in rows:
        value = row.get(column)
        if isinstance(value, str) and value:
            found.append(value)
    return found


def _first_result_rows(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    result = payload.get("result")
    if not isinstance(result, list) or not result:
        return []
    first = result[0]
    if not isinstance(first, dict):
        return []
    rows = first.get("results")
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _require_query_success(payload: Any) -> None:
    if not isinstance(payload, dict):
        raise EarlyVoteSyncError("D1 query response was not a JSON object")
    result = payload.get("result")
    if not isinstance(result, list):
        raise EarlyVoteSyncError("D1 query response did not include a result list")
    for item in result:
        if isinstance(item, dict) and item.get("success") is False:
            raise EarlyVoteSyncError("D1 query failed")


def _response_from_envelope_file(path: str) -> httpx.Response:
    with open(path, "rb") as handle:
        body = handle.read()
    return httpx.Response(200, content=body)


def _build_store(*, apply: bool, stored_hash: str = "") -> EarlyVoteStore:
    """Dry runs never open Cloudflare. ``stored_hash`` is only for a second dry run."""
    if not apply:
        text = stored_hash.strip()
        return EmptyHashStore(stored=text or None)
    config = _cloudflare_config_from_env()
    if config is None:
        raise EarlyVoteSyncError(
            "apply requires CLOUDFLARE_ACCOUNT_ID, CLOUDFLARE_API_TOKEN, "
            "and CLOUDFLARE_KV_NAMESPACE_ID"
        )
    account_id, api_token, namespace_id = config
    return CloudflareEarlyVoteStore(
        account_id=account_id,
        api_token=api_token,
        kv_namespace_id=namespace_id,
    )


def _load_response(
    election_id: str,
    envelope_file: str,
    election_date: str,
    backend: str,
    payload_kind: EarlyVotePayloadKind,
) -> Any:
    if envelope_file:
        return _response_from_envelope_file(envelope_file)
    if not election_date:
        raise EarlyVoteSyncError("pass --envelope-file or --date")
    http_backend: HttpBackend = "cloudscraper" if backend == "cloudscraper" else "httpx"
    parsed = parse_mdy(election_date)
    if payload_kind == "statewide_roster":
        return pull_statewide_roster_file(
            election_id,
            parsed,
            http_backend=http_backend,
        )
    return pull_early_vote_file(
        election_id,
        parsed,
        http_backend=http_backend,
    )


def _parse_payload_kind(value: str) -> EarlyVotePayloadKind:
    if value == "turnout":
        return "turnout"
    if value == "statewide_roster":
        return "statewide_roster"
    raise EarlyVoteSyncError("payload_kind must be turnout or statewide_roster")


def main(argv: list[str] | None = None) -> int:
    """CLI entry. ``--dry-run`` never creates Cloudflare resources."""
    parser = argparse.ArgumentParser(description="Sync one Civix early-vote file to D1 and KV.")
    parser.add_argument("--election-id", required=True, help="source_election_id string")
    parser.add_argument(
        "--sync-mode",
        default="single",
        choices=["single", "backfill", "latest"],
        help="single date, all published EV dates, or latest EV date from Civix index",
    )
    parser.add_argument("--date", default="", help="Early-vote file date MM/DD/YYYY")
    parser.add_argument("--envelope-file", default="", help="Local Civix envelope JSON")
    parser.add_argument("--dry-run", action="store_true", help="Hash and print the plan")
    parser.add_argument("--apply", action="store_true", help="Write D1, then bump KV")
    parser.add_argument(
        "--defer-kv-bump",
        action="store_true",
        help="Write D1 and precompute only; skip KV bump (finalize after backfill)",
    )
    parser.add_argument(
        "--stored-hash",
        default="",
        help="Hash from an earlier run of this file, so a second dry run can skip",
    )
    parser.add_argument("--http-backend", default="httpx", choices=["httpx", "cloudscraper"])
    parser.add_argument(
        "--payload-kind",
        default="turnout",
        choices=["turnout", "statewide_roster"],
        help="turnout JSON (EVR_EARLYVOTING) or statewide roster CSV (EVR_STATEWIDE)",
    )
    args = parser.parse_args(argv)
    if args.dry_run == args.apply:
        parser.error("pass exactly one of --dry-run or --apply")
    if args.apply and args.stored_hash:
        parser.error("--stored-hash is only used with --dry-run")

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    payload_kind = _parse_payload_kind(args.payload_kind)
    sync_mode = args.sync_mode
    store: EarlyVoteStore | None = None
    try:
        from texas_turnout_scraper.early_vote_backfill import (
            find_election_ev_dates,
            format_backfill_report,
            latest_loadable_ev_date,
            latest_published_ev_date,
            plan_backfill_load_dates,
            run_backfill,
        )

        store = _build_store(apply=args.apply, stored_hash=args.stored_hash)
        if sync_mode == "backfill":
            _name, _election_day, ev_dates = find_election_ev_dates(args.election_id)
            plan = plan_backfill_load_dates(
                args.election_id,
                ev_dates,
                http_backend=args.http_backend,
            )
            backfill = run_backfill(
                args.election_id,
                store,
                plan,
                dry_run=args.dry_run,
                payload_kind=payload_kind,
                http_backend=args.http_backend,
                defer_kv_bump=args.defer_kv_bump,
            )
            sys.stdout.write(format_backfill_report(backfill) + "\n")
            return backfill.exit_code
        election_date = args.date
        if sync_mode == "latest":
            _name, _election_day, ev_dates = find_election_ev_dates(args.election_id)
            if payload_kind == "statewide_roster":
                latest = latest_loadable_ev_date(
                    args.election_id,
                    ev_dates,
                    http_backend=args.http_backend,
                )
            else:
                latest = latest_published_ev_date(ev_dates)
            election_date = latest.strftime("%m/%d/%Y")
        response = _load_response(
            args.election_id,
            args.envelope_file,
            election_date,
            args.http_backend,
            payload_kind,
        )
        ev_date_iso: str | None = None
        if payload_kind == "statewide_roster" and election_date:
            ev_date_iso = parse_mdy(election_date).isoformat()
        result = sync_early_vote(
            args.election_id,
            response,
            store,
            dry_run=args.dry_run,
            payload_kind=payload_kind,
            ev_date_iso=ev_date_iso,
            defer_kv_bump=args.defer_kv_bump,
        )
    except (EarlyVoteCapError, EarlyVoteSyncError, ValueError) as exc:
        logger.error("%s", exc)
        return 1
    finally:
        closer = getattr(store, "close", None)
        if closer is not None:
            closer()

    sys.stdout.write(format_sync_report(result) + "\n")
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
