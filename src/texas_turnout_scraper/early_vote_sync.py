"""Daily Civix early-vote sync into one D1 database per election.

The GitHub Action pulls one ``EVR_EARLYVOTING`` file, unwraps the
``{"upload": "<base64>"}`` envelope, and SHA-256-hashes those decoded bytes.
A matching stored hash exits without a write. A new file is loaded into that
election's D1 database, and only then does KV ``ev:<election>:version`` bump.

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
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal, Protocol
from urllib.parse import quote

import httpx

from texas_turnout_scraper.civix.envelope import _decode_envelope
from texas_turnout_scraper.civix.evr import API_PREFIX, BASE_URL
from texas_turnout_scraper.civix.ids import require_election_id
from texas_turnout_scraper.http_transport import HttpBackend, PacedHttpClient

logger = logging.getLogger(__name__)

# Cloudflare's published paid-plan cap is 10 GB (decimal), not 10 GiB.
# https://developers.cloudflare.com/d1/platform/limits/
D1_DATABASE_CAP_BYTES = 10_000_000_000

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
    source_election_id TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL,
    byte_length INTEGER NOT NULL,
    chunk_count INTEGER NOT NULL
)
""".strip()

_CHUNK_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS early_vote_chunk (
    source_election_id TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    payload_b64 TEXT NOT NULL,
    PRIMARY KEY (source_election_id, chunk_index)
)
""".strip()

SyncStatus = Literal["unchanged", "dry_run", "wrote"]


class EarlyVoteCapError(RuntimeError):
    """The load would pass the 10 GB D1 per-database cap."""


class EarlyVoteSyncError(RuntimeError):
    """The sync could not finish. KV was not bumped after a failed D1 write."""


@dataclass(frozen=True)
class D1Plan:
    """Statements a dry run would send. SQL text only — no bound file bytes."""

    statements: tuple[tuple[str, int], ...]
    chunk_rows: int
    file_rows: int
    source_rows: int | None
    decoded_bytes: int
    cap_bytes: int
    within_cap: bool


@dataclass(frozen=True)
class SyncResult:
    """Outcome of one election sync. ``decoded_bytes`` is a count, not the file."""

    election_id: str
    sha256: str
    decoded_bytes: int
    status: SyncStatus
    exit_code: int
    version: str | None = None
    plan: D1Plan | None = None


class EarlyVoteStore(Protocol):
    """Hash read, D1 load, and the KV version bump."""

    def stored_hash(self, election_id: str) -> str | None:
        """Return the last hash stored after a successful version bump."""

    def write_database(self, election_id: str, decoded: bytes, digest: str) -> None:
        """Replace this election's file in its one D1 database."""

    def bump_version(self, election_id: str, digest: str) -> str:
        """Bump ``ev:<election>:version`` and then store ``digest``."""


class EmptyHashStore:
    """No Cloudflare calls.

    ``stored`` is an optional hash from an earlier run of the same file, so a
    second dry run can take the unchanged path without reading KV.
    Write methods refuse so a dry run cannot create a database.
    """

    def __init__(self, stored: str | None = None) -> None:
        self._stored = stored

    def stored_hash(self, election_id: str) -> str | None:
        return self._stored

    def write_database(self, election_id: str, decoded: bytes, digest: str) -> None:
        raise EarlyVoteSyncError("refusing to write D1 without --apply")

    def bump_version(self, election_id: str, digest: str) -> str:
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


def sha256_decoded(decoded: bytes) -> str:
    """SHA-256 hex digest of decoded EVR bytes (after the base64 unwrap)."""
    return hashlib.sha256(decoded).hexdigest()


def assert_within_cap(byte_length: int, election_id: str) -> None:
    """Refuse a load over 10 GB. Equal to the cap is still allowed.

    Does not create a database and does not open a second one.
    """
    if byte_length > D1_DATABASE_CAP_BYTES:
        raise EarlyVoteCapError(
            f"early-vote file for election {election_id} is {byte_length} bytes, "
            f"over the {D1_DATABASE_CAP_BYTES} byte D1 cap"
        )


def iter_chunk_b64(decoded: bytes) -> list[str]:
    """Split decoded bytes into base64 chunks that fit a D1 bound parameter."""
    chunks: list[str] = []
    for start in range(0, len(decoded), CHUNK_RAW_BYTES):
        piece = decoded[start : start + CHUNK_RAW_BYTES]
        chunks.append(base64.b64encode(piece).decode("ascii"))
    return chunks


_CHUNK_INSERT_SQL = (
    "INSERT INTO early_vote_chunk (source_election_id, chunk_index, payload_b64) VALUES (?, ?, ?)"
)
_FILE_INSERT_SQL = (
    "INSERT INTO early_vote_file "
    "(source_election_id, sha256, byte_length, chunk_count) "
    "VALUES (?, ?, ?, ?)"
)


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


def build_d1_statements(
    election_id: str, decoded: bytes, digest: str
) -> list[tuple[str, list[str]]]:
    """D1 statements for one election. Chunk parameters hold the file and stay off logs."""
    election_id = require_election_id(election_id)
    chunks = iter_chunk_b64(decoded)
    statements: list[tuple[str, list[str]]] = [
        (_FILE_TABLE_SQL, []),
        (_CHUNK_TABLE_SQL, []),
        ("DELETE FROM early_vote_chunk WHERE source_election_id = ?", [election_id]),
        ("DELETE FROM early_vote_file WHERE source_election_id = ?", [election_id]),
    ]
    statements.extend(
        (_CHUNK_INSERT_SQL, [election_id, str(index), payload])
        for index, payload in enumerate(chunks)
    )
    statements.append(
        (
            _FILE_INSERT_SQL,
            [election_id, digest, str(len(decoded)), str(len(chunks))],
        )
    )
    return statements


def summarize_d1_plan(election_id: str, decoded: bytes, digest: str) -> D1Plan:
    """Statement text and row counts for a dry run. Bound parameters are dropped."""
    election_id = require_election_id(election_id)
    statements = build_d1_statements(election_id, decoded, digest)
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
    return D1Plan(
        statements=tuple((sql, counts[sql]) for sql in order),
        chunk_rows=chunk_rows,
        file_rows=file_rows,
        source_rows=source_row_count(decoded),
        decoded_bytes=len(decoded),
        cap_bytes=D1_DATABASE_CAP_BYTES,
        within_cap=len(decoded) <= D1_DATABASE_CAP_BYTES,
    )


def sync_early_vote(
    election_id: str,
    response: Any,
    store: EarlyVoteStore,
    *,
    dry_run: bool = False,
) -> SyncResult:
    """Hash the unwrapped file and write D1, then KV, only when the hash is new.

    ``response`` is a Civix HTTP response (``.text`` and ``.json()``). The hash
    covers ``_decode_envelope`` bytes, not the JSON envelope text.
    """
    election_id = require_election_id(election_id)
    decoded = _decode_envelope(response)
    digest = sha256_decoded(decoded)
    if store.stored_hash(election_id) == digest:
        logger.info(
            "early-vote unchanged election_id=%s sha256=%s decoded_bytes=%s",
            election_id,
            digest,
            len(decoded),
        )
        return SyncResult(
            election_id=election_id,
            sha256=digest,
            decoded_bytes=len(decoded),
            status="unchanged",
            exit_code=0,
        )

    assert_within_cap(len(decoded), election_id)
    if dry_run:
        plan = summarize_d1_plan(election_id, decoded, digest)
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
            plan=plan,
        )

    store.write_database(election_id, decoded, digest)
    version = store.bump_version(election_id, digest)
    logger.info(
        "early-vote wrote election_id=%s sha256=%s decoded_bytes=%s version=%s",
        election_id,
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
        version=version,
    )


def format_sync_report(result: SyncResult) -> str:
    """Operator report. Counts and ids only — never the file body."""
    lines = [
        f"election_id={result.election_id}",
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
        if plan.source_rows is not None:
            row_fields.append(f"source_rows={plan.source_rows}")
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


def pull_early_vote_file(
    election_id: str,
    election_date: date,
    *,
    client: httpx.Client | None = None,
    http_backend: HttpBackend = "httpx",
) -> Any:
    """GET the Civix early-vote file. ``electionId`` is sent as a string.

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
    if client is not None:
        response = client.get(f"{BASE_URL}{path}", params=params)
        response.raise_for_status()
        return response

    paced = PacedHttpClient(BASE_URL, backend=http_backend)
    try:
        return paced.get(path, params=params)
    finally:
        paced.close()


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
    ) -> None:
        if not account_id or not api_token or not kv_namespace_id:
            raise EarlyVoteSyncError(
                "Cloudflare account id, API token, and KV namespace id are required"
            )
        self._account_id = account_id
        self._namespace_id = kv_namespace_id
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=CLOUDFLARE_API_BASE,
            headers={"Authorization": f"Bearer {api_token}"},
            timeout=60.0,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def stored_hash(self, election_id: str) -> str | None:
        value = self._kv_get(hash_key(election_id))
        if value is None:
            return None
        text = value.strip()
        return text or None

    def write_database(self, election_id: str, decoded: bytes, digest: str) -> None:
        election_id = require_election_id(election_id)
        assert_within_cap(len(decoded), election_id)
        database_id = self._ensure_one_database(election_id)
        statements = build_d1_statements(election_id, decoded, digest)
        chunks = statements[4:-1]
        self._statements(database_id, statements[:2])
        self._statements(database_id, statements[2:4])
        for start in range(0, len(chunks), _INSERT_BATCH):
            self._statements(database_id, chunks[start : start + _INSERT_BATCH])
        self._statements(database_id, statements[-1:])
        logger.info(
            "early-vote D1 replace election_id=%s database=%s chunks=%s decoded_bytes=%s",
            election_id,
            database_name(election_id),
            len(chunks),
            len(decoded),
        )

    def bump_version(self, election_id: str, digest: str) -> str:
        """Write the version key, then the hash key. A failed D1 write never calls this."""
        key = version_key(election_id)
        current = self._kv_get(key)
        nxt = _next_version(current)
        self._kv_put(key, nxt)
        self._kv_put(hash_key(election_id), digest)
        return nxt

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


def _load_response(election_id: str, envelope_file: str, election_date: str, backend: str) -> Any:
    if envelope_file:
        return _response_from_envelope_file(envelope_file)
    if not election_date:
        raise EarlyVoteSyncError("pass --envelope-file or --date")
    http_backend: HttpBackend = "cloudscraper" if backend == "cloudscraper" else "httpx"
    return pull_early_vote_file(
        election_id,
        parse_mdy(election_date),
        http_backend=http_backend,
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entry. ``--dry-run`` never creates Cloudflare resources."""
    parser = argparse.ArgumentParser(description="Sync one Civix early-vote file to D1 and KV.")
    parser.add_argument("--election-id", required=True, help="source_election_id string")
    parser.add_argument("--date", default="", help="Early-vote file date MM/DD/YYYY")
    parser.add_argument("--envelope-file", default="", help="Local Civix envelope JSON")
    parser.add_argument("--dry-run", action="store_true", help="Hash and print the plan")
    parser.add_argument("--apply", action="store_true", help="Write D1, then bump KV")
    parser.add_argument(
        "--stored-hash",
        default="",
        help="Hash from an earlier run of this file, so a second dry run can skip",
    )
    parser.add_argument("--http-backend", default="httpx", choices=["httpx", "cloudscraper"])
    args = parser.parse_args(argv)
    if args.dry_run == args.apply:
        parser.error("pass exactly one of --dry-run or --apply")
    if args.apply and args.stored_hash:
        parser.error("--stored-hash is only used with --dry-run")

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    store: EarlyVoteStore | None = None
    try:
        store = _build_store(apply=args.apply, stored_hash=args.stored_hash)
        response = _load_response(
            args.election_id,
            args.envelope_file,
            args.date,
            args.http_backend,
        )
        result = sync_early_vote(
            args.election_id,
            response,
            store,
            dry_run=args.dry_run,
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
