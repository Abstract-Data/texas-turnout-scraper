"""Scale smoke: chunked roster reads stay aggregate-sized in the isolate."""

from __future__ import annotations

import pytest

from texas_turnout_scraper.early_vote_d1_aggregate import (
    _MAX_TURNOUT_JSON_BYTES,
    build_aggregate_resource,
)
from texas_turnout_scraper.early_vote_sync import sha256_decoded

from .test_early_vote_d1_aggregate import _AggregateD1


def _synthetic_roster_rows(count: int) -> bytes:
    lines = [b"COUNTY,VOTER_NAME,ID_VOTER,VOTING_METHOD,PRECINCT"]
    for index in range(count):
        county = "TRAVIS" if index % 2 == 0 else "HARRIS"
        method = b"IN-PERSON" if index % 3 else b"MAIL-IN"
        lines.append(
            b",".join(
                [
                    county.encode(),
                    b"Name",
                    f"{index:010d}".encode(),
                    method,
                    b"1",
                ]
            )
        )
    return b"\n".join(lines) + b"\n"


@pytest.mark.asyncio
async def test_chunked_roster_aggregate_row_budget() -> None:
    """~ev-53815 row scale in many chunks; isolate never holds full decoded file."""
    row_count = 150_000
    decoded = _synthetic_roster_rows(row_count)
    digest = sha256_decoded(decoded)
    db = _AggregateD1(
        pointer="f53815",
        file_id="f53815",
        sha256=digest,
        decoded=decoded,
        byte_length=len(decoded),
    )

    peak_payload_b64 = 0
    original_first = db.first

    def _tracking_first(sql: str, args: tuple[object, ...]) -> object | None:
        nonlocal peak_payload_b64
        if "payload_b64" in sql and "chunk_index = ?" in sql:
            row = original_first(sql, args)
            if isinstance(row, dict) and isinstance(row.get("payload_b64"), str):
                size = len(row["payload_b64"])
                peak_payload_b64 = max(peak_payload_b64, size)
            return row
        return original_first(sql, args)

    db.first = _tracking_first  # type: ignore[method-assign]

    body = await build_aggregate_resource(db, "53815", "totals", kv_sha256=digest)
    assert body is not None
    assert body["statewide"]["total"] == row_count
    # One chunk's base64 payload should be far below full file size.
    assert peak_payload_b64 < len(decoded)
    assert peak_payload_b64 < 50_000
    # Result is aggregate-shaped (not row-level).
    assert len(body["by_county"]) == 2


@pytest.mark.asyncio
async def test_turnout_sql_path_memory_bounded() -> None:
    decoded = (
        b'{"turnout_by_county":[{"name":"TRAVIS",'
        b'"total_in_person_votes_for_election":1,"total_mail_votes_for_election":0}]}'
    )
    digest = sha256_decoded(decoded)
    db = _AggregateD1(
        pointer="f1",
        file_id="f1",
        sha256=digest,
        decoded=decoded,
    )
    peak_bound_bytes = 0
    original_sqlite_all = db._sqlite_all

    def _tracking_sqlite_all(sql: str, args: tuple[object, ...]) -> list[dict[str, object]]:
        nonlocal peak_bound_bytes
        if "json_each" in sql and args:
            bound = args[0]
            if isinstance(bound, str):
                peak_bound_bytes = max(peak_bound_bytes, len(bound.encode("utf-8")))
        return original_sqlite_all(sql, args)

    db._sqlite_all = _tracking_sqlite_all  # type: ignore[method-assign]

    body = await build_aggregate_resource(db, "53815", "totals", kv_sha256=digest)
    assert body is not None
    assert body["statewide"]["total"] == 1
    assert peak_bound_bytes == len(decoded)
    assert peak_bound_bytes < _MAX_TURNOUT_JSON_BYTES
