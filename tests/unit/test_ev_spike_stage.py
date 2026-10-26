"""Temporary spike routes: staging a day, the guards, and one hash range written to scratch D1."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Sequence
from datetime import date
from typing import Any

import pytest
from ev_api.app import ReadDeps, create_app
from ev_api.spike_stage import (
    CountyMethodCounts,
    RowScatter,
    StagePipeline,
    StageRequest,
    StreamingCanonicalizer,
    cleanup_run,
    collect_range,
    keys_digest,
    parse_stage_request,
    register_stage_routes,
    stage_day,
    write_range,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.unit.test_ev_spike_portal import (
    ACCESS,
    BEARER,
    MARKER,
    TODAY,
    Clock,
    FakeBody,
    FakeResponse,
    Recorder,
    Rosters,
    envelope_for,
    json_headers,
    make_deps,
    ok_response,
)
from texas_turnout_scraper import early_vote_sync
from texas_turnout_scraper.early_vote_roster import canonicalize_roster_csv
from texas_turnout_scraper.early_vote_roster_rows import (
    KEY_RANGES,
    ROW_SCHEMA_CREATES,
    key_ranges,
    keyed_lines,
)
from texas_turnout_scraper.early_vote_sync import _SCHEMA_CREATES

STAGE = "/spike/stage?election_id=53815&date=2026-10-20&suffix=run1"
RANGE_WRITE = "/spike/range-write?election_id=53815&date=2026-10-20&suffix=run1"
HEADER = "COUNTY,VOTER_NAME,ID_VOTER,VOTING_METHOD,PRECINCT\n"


def roster(rows: int, *, repeat: int = 1, seed: int = 0) -> bytes:
    """A synthetic statewide-shaped roster. Names are SYNTHETIC-*, never real voters."""
    lines = [HEADER]
    for index in range(rows):
        county = f"COUNTY{(index + seed) % 7:02d}"
        method = "MAIL-IN" if index % 3 == 0 else "IN-PERSON"
        vuid = f"{index + seed + 1:010d}"
        lines.extend(
            [f'{county},"{MARKER.decode()}, {index}",{vuid},{method},{index % 11:04d}\n'] * repeat
        )
    return "".join(lines).encode()


class FakeMultipart:
    def __init__(self, bucket: FakeBucket, key: str) -> None:
        self._bucket = bucket
        self._key = key
        self.parts: list[bytes] = []
        self.completed = False
        self.aborted = False

    async def upload_part(self, part_number: int, data: bytes) -> None:
        assert part_number == len(self.parts) + 1
        self.parts.append(data)

    async def complete(self) -> None:
        self.completed = True
        self._bucket.objects[self._key] = b"".join(self.parts)

    async def abort(self) -> None:
        self.aborted = True


class FakeBucket:
    """An in-memory ``StagingBucket``. A multipart object appears only once it completes."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.uploads: list[FakeMultipart] = []
        self.deleted: list[str] = []
        self.puts = 0

    async def put(self, key: str, data: bytes) -> None:
        self.puts += 1
        self.objects[key] = data

    async def multipart(self, key: str) -> FakeMultipart:
        upload = FakeMultipart(self, key)
        self.uploads.append(upload)
        return upload

    async def keys(self, prefix: str) -> list[str]:
        return sorted(key for key in self.objects if key.startswith(prefix))

    async def read(self, key: str) -> bytes | None:
        return self.objects.get(key)

    async def delete(self, key: str) -> None:
        self.deleted.append(key)
        self.objects.pop(key, None)


class FakeStatement:
    def __init__(self, sql: str) -> None:
        self.sql = sql
        self.params: tuple[object, ...] = ()

    def bind(self, *params: object) -> FakeStatement:
        self.params = params
        return self


class FakeD1:
    """A sqlite-backed D1 binding with the ``prepare``/``bind``/``batch`` surface."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.batches: list[int] = []
        self.fail_with: Exception | None = None

    def prepare(self, sql: str) -> FakeStatement:
        return FakeStatement(sql)

    async def batch(self, statements: Sequence[FakeStatement]) -> list[dict[str, object]]:
        if self.fail_with is not None:
            raise self.fail_with
        self.batches.append(len(statements))
        for statement in statements:
            self.conn.execute(statement.sql, statement.params)
        self.conn.commit()
        return [{"success": True} for _ in statements]


@pytest.fixture
def scratch() -> FakeD1:
    return FakeD1(sqlite3.connect(":memory:", check_same_thread=False))


def request_for(suffix: str = "run1", ev_date: str = "2026-10-20") -> StageRequest:
    return StageRequest("53815", ev_date, date.fromisoformat(ev_date), suffix)


def stage_client(
    fetch: Callable[..., Any],
    bucket: FakeBucket,
    *,
    scratch: Any | None = None,
    deps: ReadDeps | None = None,
    clock: Clock | None = None,
    today: date = TODAY,
) -> TestClient:
    app = FastAPI()
    active = deps or make_deps()
    register_stage_routes(
        app,
        lambda request: active,
        fetch=fetch,
        clock=clock or Clock(),
        today=lambda: today,
        staging=bucket,
        scratch=scratch,
    )
    return TestClient(app)


# --------------------------------------------------------------------------------------
# Canonicalizer: the cases the ADR names, as a table
# --------------------------------------------------------------------------------------


def _separators_case() -> bytes:
    """A row whose cells hold U+2028 and U+0085, which str.splitlines() would split on."""
    return f"a,b\n1,{chr(0x2028)}{chr(0x85)}\n".encode()


@pytest.mark.parametrize(
    ("source", "canonical"),
    [
        (b"", b""),
        (b"a,b,c\n", b"a,b,c\n"),
        (b"a,b,c", b"a,b,c\n"),
        (b"\n", b"\n"),
        (b"\na,b\n", b"\na,b\n"),
        (b"a,b\r\n1,2\r\n", b"a,b\n1,2\n"),
        (b"\xef\xbb\xbfa,b\n1,2\n", b"a,b\n1,2\n"),
        (b"a,b\n1,2\n\n3,4\n", b"a,b\n1,2\n3,4\n"),
        (b"a,b\n \t,\x0b\n", b"a,b\n \t,\x0b\n"),
        (b'a,b\n"x\ny",2\n', b'a,b\n"x\ny",2\n'),
        (b'a,b\n"he said ""hi""",2\n', b'a,b\n"he said ""hi""",2\n'),
        (b"a,b\n1,2", b"a,b\n1,2\n"),
        (_separators_case(), _separators_case()),
    ],
    ids=[
        "empty",
        "header-only",
        "header-no-terminator",
        "blank-line-only-is-an-empty-header",
        "leading-blank-line-is-the-header",
        "crlf",
        "bom",
        "blank-row-dropped",
        "whitespace-only-row-kept",
        "quoted-newline",
        "doubled-quotes",
        "no-trailing-newline",
        "unicode-separators-kept",
    ],
)
def test_named_canonicalizer_cases_match_the_reference(source: bytes, canonical: bytes) -> None:
    expected, _ = canonicalize_roster_csv(source)
    assert expected == canonical
    machine = StreamingCanonicalizer()
    streamed = b"".join(
        record.canonical
        for record in [
            *(r for index in range(len(source)) for r in machine.feed(source[index : index + 1])),
            *machine.finish(),
        ]
    )
    assert streamed == canonical


def test_county_and_method_counts_hold_no_names_or_vuids() -> None:
    counts = CountyMethodCounts.for_header(["COUNTY", "VOTER_NAME", "ID_VOTER", "VOTING_METHOD"])
    counts.add(["TRAVIS", f"{MARKER.decode()}, A", "0000000001", "mail-in"])
    counts.add(["TRAVIS", f"{MARKER.decode()}, B", "0000000002", "MAIL-IN"])
    report = json.dumps(counts.as_report())
    assert counts.as_report() == [{"county": "TRAVIS", "voting_method": "MAIL-IN", "rows": 2}]
    assert MARKER.decode() not in report and "0000000001" not in report


# --------------------------------------------------------------------------------------
# Stage
# --------------------------------------------------------------------------------------


async def test_stage_writes_the_canonical_object_and_the_segments_the_counts_claim() -> None:
    raw = roster(400)
    canonical, _ = canonicalize_roster_csv(raw)
    bucket = FakeBucket()
    parsed = request_for()
    report = await stage_day(
        Recorder(ok_response(raw, pieces=25)),
        "https://example.invalid/",
        bucket,
        parsed,
        part_bytes=4096,
        flush_bytes=512,
    )

    assert report["rows"] == 400
    assert report["sha256"] == hashlib.sha256(canonical).hexdigest()
    assert report["raw_sha256"] == hashlib.sha256(raw).hexdigest()
    assert report["canonical_bytes"] == len(canonical)
    assert bucket.objects[parsed.canonical_key] == canonical
    assert report["canonical_key"] == parsed.canonical_key
    assert sum(report["rows_per_range"]) == 400
    assert sum(report["segments_per_range"]) == report["segments_written"]
    segment_keys = [
        key for index in range(KEY_RANGES) for key in await bucket.keys(parsed.range_prefix(index))
    ]
    assert report["segments_written"] == len(segment_keys)
    assert sorted(bucket.objects) == sorted(
        [*segment_keys, parsed.canonical_key, parsed.header_key]
    )
    assert json.loads(bucket.objects[parsed.header_key]) == report["header_columns"]
    assert report["header_schema_error"] is None
    assert report["error"] is None and report["wrote_anything"] is True


async def test_stage_multipart_parts_are_the_fixed_size_except_the_last() -> None:
    raw = roster(500)
    canonical, _ = canonicalize_roster_csv(raw)
    bucket = FakeBucket()
    report = await stage_day(
        Recorder(ok_response(raw, pieces=40)),
        "https://example.invalid/",
        bucket,
        request_for(),
        part_bytes=8192,
        flush_bytes=1024,
    )
    (upload,) = bucket.uploads
    assert upload.completed and not upload.aborted
    assert len(canonical) > 8192 * 2
    assert [len(part) for part in upload.parts[:-1]] == [8192] * (len(upload.parts) - 1)
    assert len(upload.parts[-1]) <= 8192
    assert b"".join(upload.parts) == canonical
    assert report["multipart_parts"] == len(upload.parts)
    assert report["multipart_bytes"] == len(canonical)


async def test_stage_segments_split_at_the_flush_size_and_carry_every_row() -> None:
    raw = roster(300, repeat=2)
    bucket = FakeBucket()
    parsed = request_for()
    report = await stage_day(
        Recorder(ok_response(raw, pieces=12)),
        "https://example.invalid/",
        bucket,
        parsed,
        part_bytes=1 << 20,
        flush_bytes=400,
    )
    assert report["segments_written"] > KEY_RANGES
    rebuilt: list[tuple[int, int]] = []
    header = json.loads(bucket.objects[parsed.header_key])
    for index in range(KEY_RANGES):
        keys = await bucket.keys(parsed.range_prefix(index))
        collected = collect_range(header, [bucket.objects[key] for key in keys])
        low, high = (int(bound) for bound in key_ranges()[index])
        assert all(low <= digest <= high for digest, _ in collected.keys)
        rebuilt.extend(collected.keys)
    assert sorted(rebuilt) == sorted(key for key, _ in keyed_lines(raw))


async def test_an_empty_portal_file_writes_no_canonical_object() -> None:
    bucket = FakeBucket()
    report = await stage_day(
        Recorder(ok_response(b"")), "https://example.invalid/", bucket, request_for()
    )
    assert report["rows"] == 0
    assert report["canonical_bytes"] == 0
    assert report["sha256"] == hashlib.sha256(b"").hexdigest()
    assert bucket.objects == {}
    assert bucket.uploads == []
    assert report["wrote_anything"] is False


async def test_a_challenge_page_is_reported_and_nothing_is_written() -> None:
    page = b"<!DOCTYPE html><html><title>Just a moment...</title>" + b"x" * 5000
    body = FakeBody([page[:100], page[100:]])
    fetch = Recorder(FakeResponse(200, json_headers(**{"cf-mitigated": "challenge"}), body))
    bucket = FakeBucket()
    report = await stage_day(fetch, "https://example.invalid/", bucket, request_for())
    assert report["body_looks_like_html_challenge"] is True
    assert report["sha256"] is None and report["canonical_key"] is None
    assert report["rows"] == 0 and report["wrote_anything"] is False
    assert bucket.objects == {} and bucket.uploads == []
    assert body.pulled == 1


async def test_a_non_200_is_reported_and_nothing_is_written() -> None:
    headers = json_headers(**{"retry-after": "30"})
    body = FakeBody([b'{"error":"busy"}'])
    bucket = FakeBucket()
    report = await stage_day(
        Recorder(FakeResponse(503, headers, body)),
        "https://example.invalid/",
        bucket,
        request_for(),
    )
    assert report["http_status"] == 503
    assert report["sha256"] is None
    assert bucket.objects == {} and bucket.uploads == []


async def test_a_truncated_body_aborts_the_multipart_upload_and_leaves_no_object() -> None:
    body, _ = envelope_for(roster(600))
    bucket = FakeBucket()
    report = await stage_day(
        Recorder(FakeResponse(200, json_headers(), FakeBody([body[: len(body) // 2]]))),
        "https://example.invalid/",
        bucket,
        request_for(),
        part_bytes=2048,
        flush_bytes=512,
    )
    assert report["error"] == {"class": "EnvelopeError", "reason": "truncated"}
    assert report["sha256"] is None and report["canonical_key"] is None
    (upload,) = bucket.uploads
    assert upload.aborted and not upload.completed
    assert request_for().canonical_key not in bucket.objects


async def test_the_report_never_carries_row_content() -> None:
    bucket = FakeBucket()
    report = await stage_day(
        Recorder(ok_response(roster(50))), "https://example.invalid/", bucket, request_for()
    )
    assert MARKER.decode() not in json.dumps(report)
    assert "0000000001" not in json.dumps(report)


# --------------------------------------------------------------------------------------
# Route guards
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer nope"}, {"X-API-Key": "nope"}])
def test_unauthenticated_calls_touch_neither_the_portal_nor_the_bucket(
    headers: dict[str, str], scratch: FakeD1
) -> None:
    fetch = Recorder(ok_response(roster(5)))
    bucket = FakeBucket()
    client = stage_client(fetch, bucket, scratch=scratch)
    assert client.get(STAGE, headers=headers).status_code == 401
    assert client.post(f"{RANGE_WRITE}&range=0", headers=headers).status_code == 401
    assert fetch.calls == []
    assert bucket.objects == {} and bucket.deleted == [] and scratch.batches == []


def test_a_consumer_bearer_token_is_accepted() -> None:
    fetch = Recorder(ok_response(roster(5)))
    client = stage_client(fetch, FakeBucket())
    assert client.get(STAGE, headers=BEARER).status_code == 200
    assert len(fetch.calls) == 1


@pytest.mark.parametrize(
    ("query", "detail"),
    [
        ("election_id=53815&date=2026-10-26&suffix=a", "date_in_future"),
        ("election_id=53815&date=2026-10-13&suffix=a", "date_outside_calendar"),
        ("election_id=99999&date=2026-10-20&suffix=a", "election_not_allowed"),
        ("election_id=abc&date=2026-10-20&suffix=a", "bad_request"),
        ("election_id=53815&date=10/20/2026&suffix=a", "bad_request"),
        ("election_id=53815&suffix=a", "bad_request"),
        ("election_id=53815&date=2026-10-20", "bad_suffix"),
        ("election_id=53815&date=2026-10-20&suffix=", "bad_suffix"),
        ("election_id=53815&date=2026-10-20&suffix=../etc", "bad_suffix"),
        ("election_id=53815&date=2026-10-20&suffix=" + "x" * 33, "bad_suffix"),
    ],
)
def test_rejected_requests_never_reach_the_portal(query: str, detail: str) -> None:
    fetch = Recorder(ok_response(roster(5)))
    bucket = FakeBucket()
    response = stage_client(fetch, bucket).get(f"/spike/stage?{query}", headers=ACCESS)
    assert response.status_code == 400
    assert response.json() == {"detail": detail}
    assert fetch.calls == [] and bucket.objects == {}


@pytest.mark.parametrize("value", ["", "16", "-1", "0x1", "99"])
def test_a_bad_range_is_refused_before_any_read(value: str) -> None:
    bucket = FakeBucket()
    client = stage_client(Recorder(ok_response(roster(5))), bucket)
    response = client.post(f"{RANGE_WRITE}&range={value}", headers=ACCESS)
    assert response.status_code == 400
    assert response.json() == {"detail": "bad_range"}


def test_one_portal_call_per_ten_seconds_and_rejects_do_not_use_the_slot() -> None:
    clock = Clock()
    fetch = Recorder(ok_response(roster(5)))
    client = stage_client(fetch, FakeBucket(), clock=clock)
    assert client.get(STAGE, headers=ACCESS).status_code == 200
    clock.now += 3
    refused = client.get(STAGE, headers=ACCESS)
    assert refused.status_code == 429
    assert refused.json() == {"detail": "rate_limited"}
    assert refused.headers["retry-after"] == "8"
    assert len(fetch.calls) == 1
    assert client.get(STAGE).status_code == 401
    clock.now += 7
    fetch.response = ok_response(roster(5))
    assert client.get(STAGE, headers=ACCESS).status_code == 200
    assert len(fetch.calls) == 2


def test_cleanup_deletes_only_this_runs_objects_and_makes_no_portal_call() -> None:
    fetch = Recorder(ok_response(roster(5)))
    bucket = FakeBucket()
    bucket.objects.update(
        {
            "staging/spike-run1/2026-10-20/file.csv": b"x",
            "staging/spike-run1/2026-10-20/00/000000": b"y",
            "staging/spike-run1/2026-10-21/file.csv": b"keep",
            "staging/spike-other/2026-10-20/file.csv": b"keep",
        }
    )
    response = stage_client(fetch, bucket).get(f"{STAGE}&cleanup=1", headers=ACCESS)
    assert response.status_code == 200
    assert response.json()["objects_deleted"] == 2
    assert sorted(bucket.objects) == [
        "staging/spike-other/2026-10-20/file.csv",
        "staging/spike-run1/2026-10-21/file.csv",
    ]
    assert fetch.calls == []


def test_the_mounted_app_exposes_both_routes_behind_the_same_auth() -> None:
    client = TestClient(create_app(make_deps()))
    assert client.get(STAGE).status_code == 401
    assert client.post(f"{RANGE_WRITE}&range=0").status_code == 401
    paths = {route.path for route in client.app.routes}  # type: ignore[attr-defined]
    assert {"/spike/stage", "/spike/range-write"} <= paths


def test_stage_reports_a_missing_staging_binding_without_fetching() -> None:
    fetch = Recorder(ok_response(roster(5)))
    app = FastAPI()
    deps = make_deps()
    register_stage_routes(app, lambda request: deps, fetch=fetch, today=lambda: TODAY)
    response = TestClient(app).get(STAGE, headers=ACCESS)
    assert response.json() == {"error": "no_staging_binding"}
    assert fetch.calls == []


def test_parse_stage_request_builds_the_run_prefix() -> None:
    parsed = parse_stage_request("53815", "2026-10-20", "run1", today=TODAY)
    assert parsed.prefix == "staging/spike-run1/2026-10-20"
    assert parsed.canonical_key == "staging/spike-run1/2026-10-20/file.csv"
    assert parsed.segment_key(3, 7) == "staging/spike-run1/2026-10-20/03/000007"
    assert parsed.range_prefix(15) == "staging/spike-run1/2026-10-20/15/"


# --------------------------------------------------------------------------------------
# Stored-hash comparison
# --------------------------------------------------------------------------------------


@pytest.fixture
def stored_db(monkeypatch: pytest.MonkeyPatch) -> Any:
    from tests.unit.test_early_vote_roster_stream import _Db, _store_day

    monkeypatch.setattr(early_vote_sync, "CHUNK_RAW_BYTES", 16)
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    for sql in _SCHEMA_CREATES:
        conn.execute(sql)
    _store_day(conn, "2026-10-20", canonicalize_roster_csv(roster(3))[0])
    return _Db(conn)


def test_the_stored_day_hash_is_compared_with_the_canonical_hash(stored_db: Any) -> None:
    raw = roster(3)
    canonical, _ = canonicalize_roster_csv(raw)
    deps = make_deps(Rosters({"53815": stored_db}))

    same = stage_client(Recorder(ok_response(raw)), FakeBucket(), deps=deps)
    body = same.get(STAGE, headers=ACCESS).json()
    assert body["stored_sha256"] == hashlib.sha256(canonical).hexdigest()
    assert body["sha256"] == body["stored_sha256"]
    assert body["stored_matches_canonical"] is True

    changed = stage_client(Recorder(ok_response(roster(4))), FakeBucket(), deps=deps)
    other = changed.get(STAGE, headers=ACCESS).json()
    assert other["stored_matches_canonical"] is False


def test_no_roster_binding_is_reported_not_guessed() -> None:
    client = stage_client(Recorder(ok_response(roster(3))), FakeBucket())
    body = client.get(STAGE, headers=ACCESS).json()
    assert body["stored_lookup"] == "no_binding"
    assert body["stored_sha256"] is None and body["stored_matches_canonical"] is None


# --------------------------------------------------------------------------------------
# Range write
# --------------------------------------------------------------------------------------


async def _staged(bucket: FakeBucket, raw: bytes, *, flush_bytes: int = 2048) -> StageRequest:
    parsed = request_for()
    await stage_day(
        Recorder(ok_response(raw, pieces=20)),
        "https://example.invalid/",
        bucket,
        parsed,
        part_bytes=1 << 20,
        flush_bytes=flush_bytes,
    )
    return parsed


async def test_range_write_reproduces_keyed_lines_and_writes_the_rows(scratch: FakeD1) -> None:
    raw = roster(200, repeat=3)
    bucket = FakeBucket()
    parsed = await _staged(bucket, raw)
    expected = {key for key, _ in keyed_lines(raw)}

    sampled: set[tuple[int, int]] = set()
    written = 0
    for index in range(KEY_RANGES):
        report = await write_range(bucket, scratch, parsed, index)
        assert "reparse_error" not in report
        assert report["validated_keys_match_scatter"] is True
        assert report["bound_param_accepted"] is True
        assert report["max_bound_param_bytes"] > 0
        assert report["rows_per_second"] is None or report["rows_per_second"] > 0
        sampled |= {(digest, occ) for digest, occ in report["first_keys"]}
        written += int(report["rows_written"])
    assert written == 600

    rows = scratch.conn.execute(
        "SELECT COUNT(*) AS n, COUNT(DISTINCT row_hash) AS h FROM early_vote_roster_row"
    ).fetchone()
    assert rows[0] == 600
    assert rows[1] == 200
    stored = {
        (digest, occ)
        for digest, occ in scratch.conn.execute(
            "SELECT row_hash, occ FROM early_vote_roster_row"
        ).fetchall()
    }
    assert stored == expected
    assert sampled <= expected


async def test_range_write_verify_mode_reports_the_pairs_and_writes_nothing(
    scratch: FakeD1,
) -> None:
    raw = roster(60, repeat=2)
    bucket = FakeBucket()
    parsed = await _staged(bucket, raw, flush_bytes=256)
    header = json.loads(bucket.objects[parsed.header_key])

    digests: list[str] = []
    for index in range(KEY_RANGES):
        report = await write_range(bucket, scratch, parsed, index, verify=True)
        assert report["verify_only"] is True
        assert len(report["first_keys"]) <= 5 and len(report["last_keys"]) <= 5
        keys = await bucket.keys(parsed.range_prefix(index))
        collected = collect_range(header, [bucket.objects[key] for key in keys])
        assert report["keys_sha256"] == keys_digest(collected.keys)
        assert report["rows"] == len(collected.keys)
        digests.append(str(report["keys_sha256"]))
    assert scratch.batches == []
    assert len(set(digests)) > 1


async def test_range_write_reports_a_d1_failure_as_a_class_only(scratch: FakeD1) -> None:
    bucket = FakeBucket()
    parsed = await _staged(bucket, roster(40))
    scratch.fail_with = RuntimeError("SYNTHETIC-SECRET-DETAIL")
    populated = [
        index for index in range(KEY_RANGES) if await bucket.keys(parsed.range_prefix(index))
    ]
    report = await write_range(bucket, scratch, parsed, populated[0])
    assert report["bound_param_accepted"] is False
    assert report["write_error"] == "RuntimeError"
    assert "SYNTHETIC-SECRET-DETAIL" not in json.dumps(report)


async def test_range_write_without_staged_objects_reports_it(scratch: FakeD1) -> None:
    report = await write_range(FakeBucket(), scratch, request_for(), 0)
    assert report["error"] == "no_staged_header"
    assert scratch.batches == []


async def test_an_empty_range_needs_no_validation_and_no_write(scratch: FakeD1) -> None:
    bucket = FakeBucket()
    parsed = await _staged(bucket, roster(1))
    empty = [
        index for index in range(KEY_RANGES) if not await bucket.keys(parsed.range_prefix(index))
    ]
    report = await write_range(bucket, scratch, parsed, empty[0])
    assert report["rows"] == 0
    assert report["keys_sha256"] == keys_digest([])
    assert "sqlmodel_import_ms" not in report
    assert scratch.batches == []


async def test_range_write_creates_the_row_tables_it_needs(scratch: FakeD1) -> None:
    bucket = FakeBucket()
    parsed = await _staged(bucket, roster(30))
    for index in range(KEY_RANGES):
        await write_range(bucket, scratch, parsed, index)
    tables = {
        name
        for (name,) in scratch.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    assert {"early_vote_roster_row", "early_vote_roster_reject"} <= tables
    assert len(ROW_SCHEMA_CREATES) == 3


async def test_a_bad_row_is_quarantined_as_a_reject_not_dropped(scratch: FakeD1) -> None:
    raw = (HEADER + "TRAVIS,SYNTHETIC-A,not-a-vuid,MAIL-IN,0001\n").encode()
    bucket = FakeBucket()
    parsed = await _staged(bucket, raw)
    total = 0
    for index in range(KEY_RANGES):
        report = await write_range(bucket, scratch, parsed, index)
        total += int(report.get("validated_rejects", 0))
    assert total == 1
    assert scratch.conn.execute("SELECT COUNT(*) FROM early_vote_roster_reject").fetchone()[0] == 1


async def test_cleanup_removes_every_object_stage_wrote() -> None:
    bucket = FakeBucket()
    parsed = await _staged(bucket, roster(100), flush_bytes=256)
    assert len(bucket.objects) > KEY_RANGES
    report = await cleanup_run(bucket, parsed)
    assert report["objects_deleted"] == len(bucket.deleted)
    assert bucket.objects == {}


async def test_the_pipeline_only_opens_an_upload_once_a_part_is_full() -> None:
    bucket = FakeBucket()
    pipeline = StagePipeline(bucket, request_for(), part_bytes=1 << 20, flush_bytes=1 << 20)
    pipeline.take(roster(5))
    await pipeline.drain()
    assert bucket.uploads == [] and bucket.objects == {}
    await pipeline.finish()
    assert len(bucket.uploads) == 1 and bucket.uploads[0].completed


def test_scatter_rows_and_segment_counts_agree_with_what_it_handed_out() -> None:
    scatter = RowScatter(["COUNTY", "ID_VOTER"], flush_bytes=1)
    handed = [scatter.add(["TRAVIS", f"{index:010d}"])[1] for index in range(20)]
    assert all(segment is not None for segment in handed)
    assert sum(scatter.rows_per_range) == 20
    assert sum(scatter.segments_per_range) == 20
    assert list(scatter.flush_all()) == []
