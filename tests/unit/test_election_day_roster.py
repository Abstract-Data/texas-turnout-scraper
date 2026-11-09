"""ADR-018: Election Day rosters share the early-vote tables, marked by ``vote_period``."""

from __future__ import annotations

import base64
import hashlib
import io
import sqlite3
import zipfile
from datetime import date
from typing import Any, cast

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from typing_extensions import override

from tests.unit.test_early_vote_backfill import STATEWIDE_FIXTURE, _BackfillStore
from tests.unit.test_early_vote_cumulative import DAYS, KV, _db, _get
from tests.unit.test_early_vote_roster_stream import DAY1, DAY2, _Db, _store_day
from tests.unit.test_ev_roster_api import AGGREGATES, _Kv, _Rosters
from texas_turnout_scraper.civix.evr import (
    API_PREFIX,
    BASE_URL,
    STATEWIDE_ED_VOTER_CSV,
    EdStatewideReportCorruptError,
    EdStatewideReportMissingError,
    parse_ed_statewide_voter_records,
    read_ed_statewide_voter_csv,
)
from texas_turnout_scraper.early_vote_backfill import (
    DEFAULT_NOV_2026_GENERAL_ID,
    plan_backfill_load_dates,
    run_backfill,
)
from texas_turnout_scraper.early_vote_d1_precompute import PRECOMPUTE_SCHEMA_CREATES
from texas_turnout_scraper.early_vote_d1_precompute_read import (
    build_precomputed_aggregate_resource,
)
from texas_turnout_scraper.early_vote_d1_read import _DAY_LIST_SQL
from texas_turnout_scraper.early_vote_roster import canonicalize_roster_csv
from texas_turnout_scraper.early_vote_sync import (
    _DAY_TABLE_SQL,
    _DAY_UPSERT_SQL,
    _SCHEMA_CREATES,
    DAY_COLUMNS_SQL,
    CloudflareEarlyVoteStore,
    EmptyHashStore,
    day_migration_statements,
    sha256_decoded,
)
from texas_turnout_scraper.first_seen import PARTITIONS

EARLY_DAY = date(2026, 10, 14)
ELECTION_DAY = date(2026, 11, 3)
TODAY = date(2026, 11, 9)
GET_FILE = f"{BASE_URL}{API_PREFIX}/getFile"

EARLY_HEADER = "COUNTY,VOTER_NAME,ID_VOTER,VOTING_METHOD,PRECINCT"
ED_HEADER = "COUNTY_NAME,VOTER_NAME,ID_VOTER,VOTING_METHOD,PRECINCT"
ROWS = (
    '"TRAVIS","DOE, ONE","0000000001","IN-PERSON","100"\n'
    '"BEXAR","DOE, TWO","0000000002","MAIL-IN","200"\n'
)
# The canonical form of ROWS: minimal quoting, LF endings.
CANONICAL_ROWS = (
    'TRAVIS,"DOE, ONE",0000000001,IN-PERSON,100\nBEXAR,"DOE, TWO",0000000002,MAIL-IN,200\n'
)


def _ed_zip(*, with_csv: bool = True, rows: str = ROWS) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        if with_csv:
            zf.writestr(STATEWIDE_ED_VOTER_CSV, f"{ED_HEADER}\n{rows}")
        zf.writestr("STATEWIDE_POLLING_PLACE_INFO.csv", "COUNTY,POLL PLACE ID\nTRAVIS,1\n")
    return buffer.getvalue()


def _corrupt_ed_zip() -> bytes:
    """A ZIP whose central directory is intact but whose voter CSV fails its CRC."""
    return _ed_zip().replace(b"DOE, ONE", b"DOE, 0NE")


def _zip_of_voter_csv(voter_csv: bytes) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr(STATEWIDE_ED_VOTER_CSV, voter_csv)
    return buffer.getvalue()


def _ed_zip_with_voter_csv_field(offset: int, value: int) -> bytes:
    """Overwrite one byte of the voter CSV's central-directory entry."""
    payload = bytearray(_ed_zip())
    payload[payload.index(b"PK\x01\x02") + offset] = value
    return bytes(payload)


ENCRYPTED_FLAG = _ed_zip_with_voter_csv_field(8, 1)
UNKNOWN_COMPRESSION = _ed_zip_with_voter_csv_field(10, 99)
UNSUPPORTED_VERSION = _ed_zip_with_voter_csv_field(6, 200)


def _envelope(payload: bytes) -> httpx.Response:
    return httpx.Response(200, json={"upload": base64.b64encode(payload).decode("ascii")})


class _KeepingStore(_BackfillStore):
    """Records the bytes written for each day, which the shared fake does not keep."""

    def __init__(self) -> None:
        super().__init__()
        self.written: dict[str, bytes] = {}

    @override
    def write_database(self, election_id: str, decoded: bytes, digest: str, **kwargs: Any) -> None:
        super().write_database(election_id, decoded, digest, **kwargs)
        self.written[kwargs["ev_date_iso"]] = decoded


def _mock_early(router: respx.MockRouter) -> None:
    router.get(GET_FILE, params={"type": "EVR_STATEWIDE", "electionDate": "10/14/2026"}).mock(
        return_value=httpx.Response(200, content=STATEWIDE_FIXTURE.read_bytes())
    )


def _mock_election_day(router: respx.MockRouter, response: httpx.Response) -> respx.Route:
    return router.get(
        GET_FILE,
        params={
            "type": "EVR_STATEWIDE_ELECTIONDAY",
            "electionId": DEFAULT_NOV_2026_GENERAL_ID,
            "electionDate": "11/03/2026",
        },
    ).mock(return_value=response)


def _plan() -> Any:
    return plan_backfill_load_dates(
        DEFAULT_NOV_2026_GENERAL_ID, (EARLY_DAY,), today=TODAY, election_day=ELECTION_DAY
    )


def _backfill(store: _BackfillStore) -> Any:
    return run_backfill(DEFAULT_NOV_2026_GENERAL_ID, store, _plan())


def test_early_vote_canonical_bytes_and_sha_are_unchanged() -> None:
    canonical, summary = canonicalize_roster_csv(f"{EARLY_HEADER}\r\n{ROWS}".encode())

    assert canonical == f"{EARLY_HEADER}\n{CANONICAL_ROWS}".encode()
    assert hashlib.sha256(canonical).hexdigest() == (
        hashlib.sha256(f"{EARLY_HEADER}\n{CANONICAL_ROWS}".encode()).hexdigest()
    )
    assert summary.columns == tuple(EARLY_HEADER.split(","))


def test_county_name_becomes_county_and_hashes_like_an_early_vote_file() -> None:
    early, _ = canonicalize_roster_csv(f"{EARLY_HEADER}\n{ROWS}".encode())
    renamed, summary = canonicalize_roster_csv(f"{ED_HEADER}\n{ROWS}".encode())

    assert renamed == early
    assert sha256_decoded(renamed) == sha256_decoded(early)
    assert summary.columns[0] == "COUNTY"
    assert summary.voting_method_counts == (("IN-PERSON", 1), ("MAIL-IN", 1))


def test_the_rename_matches_the_stripped_name_without_regard_to_case() -> None:
    renamed, _ = canonicalize_roster_csv(
        b" county_name ,VOTER_NAME,ID_VOTER,VOTING_METHOD\nT,a,1,x\n"
    )

    assert renamed.splitlines()[0] == b"COUNTY,VOTER_NAME,ID_VOTER,VOTING_METHOD"


def test_only_county_name_is_renamed() -> None:
    header = "COUNTY_NAME,VOTER_NAME,ID_VOTER,VOTING_METHOD,PRECINCT_NAME"
    renamed, _ = canonicalize_roster_csv(f"{header}\nT,a,1,x,p\n".encode())

    assert renamed.splitlines()[0] == b"COUNTY,VOTER_NAME,ID_VOTER,VOTING_METHOD,PRECINCT_NAME"


def test_the_plan_adds_election_day_once_it_has_passed() -> None:
    plan = plan_backfill_load_dates("53815", (EARLY_DAY,), today=TODAY, election_day=ELECTION_DAY)

    assert plan.load_dates == (EARLY_DAY, ELECTION_DAY)
    assert plan.skipped_future_dates == ()
    assert plan.vote_period(EARLY_DAY) == "early"
    assert plan.vote_period(ELECTION_DAY) == "election_day"


def test_the_plan_leaves_election_day_out_until_it_has_passed() -> None:
    plan = plan_backfill_load_dates(
        "53815", (EARLY_DAY,), today=date(2026, 11, 2), election_day=ELECTION_DAY
    )

    assert plan.load_dates == (EARLY_DAY,)
    assert plan.skipped_future_dates == ("2026-11-03",)


def test_the_plan_never_repeats_an_election_day_already_in_the_calendar() -> None:
    plan = plan_backfill_load_dates(
        "53815", (EARLY_DAY, ELECTION_DAY), today=TODAY, election_day=ELECTION_DAY
    )

    assert plan.load_dates == (EARLY_DAY, ELECTION_DAY)
    assert plan.calendar_ev_days_total == 2


def test_without_an_election_day_every_date_is_early() -> None:
    plan = plan_backfill_load_dates("53815", (EARLY_DAY,), today=TODAY)

    assert plan.load_dates == (EARLY_DAY,)
    assert plan.vote_period(EARLY_DAY) == "early"


def test_the_helper_returns_the_voter_csv_bytes() -> None:
    assert read_ed_statewide_voter_csv(_ed_zip()) == f"{ED_HEADER}\n{ROWS}".encode()


@pytest.mark.parametrize("payload", [b"", b"not a zip", _ed_zip(with_csv=False)])
def test_the_helper_rejects_a_payload_without_the_voter_csv(payload: bytes) -> None:
    with pytest.raises(EdStatewideReportMissingError):
        read_ed_statewide_voter_csv(payload)


def test_the_record_parser_reads_through_the_helper() -> None:
    records = parse_ed_statewide_voter_records(
        _ed_zip(), election_id="53815", report_date=ELECTION_DAY
    )
    assert [r.county for r in records] == ["TRAVIS", "BEXAR"]
    with pytest.raises(ValueError, match="missing"):
        parse_ed_statewide_voter_records(
            _ed_zip(with_csv=False), election_id="53815", report_date=ELECTION_DAY
        )


@respx.mock
def test_the_election_day_zip_reaches_the_store_marked_election_day() -> None:
    _mock_early(respx.mock)
    route = _mock_election_day(respx.mock, _envelope(_ed_zip()))
    store = _KeepingStore()

    result = _backfill(store)

    assert route.call_count == 1
    assert result.days_wrote == 2
    assert result.skipped_empty_dates == () and result.failed_dates == ()
    assert store.vote_periods == {"2026-10-14": "early", "2026-11-03": "election_day"}
    assert store.written["2026-11-03"] == f"{EARLY_HEADER}\n{CANONICAL_ROWS}".encode()


@respx.mock
def test_the_early_day_is_pulled_as_an_early_vote_report() -> None:
    _mock_early(respx.mock)
    _mock_election_day(respx.mock, _envelope(_ed_zip()))
    store = _KeepingStore()

    _backfill(store)

    types = [call.request.url.params["type"] for call in respx.calls]
    assert types == ["EVR_STATEWIDE", "EVR_STATEWIDE_ELECTIONDAY"]


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(404),
        httpx.Response(200, content=b""),
        _envelope(b"not a zip"),
        _envelope(_ed_zip(with_csv=False)),
        _envelope(_ed_zip(rows="")),
    ],
    ids=["http-404", "empty-body", "not-a-zip", "zip-without-csv", "zip-without-rows"],
)
@respx.mock
def test_a_report_the_portal_has_not_published_is_skipped_empty(response: httpx.Response) -> None:
    _mock_early(respx.mock)
    _mock_election_day(respx.mock, response)
    store = _KeepingStore()

    result = _backfill(store)

    assert result.skipped_empty_dates == ("2026-11-03",)
    assert result.failed_dates == ()
    assert result.days_wrote == 1
    assert "2026-11-03" not in store.vote_periods


@pytest.mark.parametrize(
    "payload",
    [_corrupt_ed_zip(), ENCRYPTED_FLAG, UNKNOWN_COMPRESSION, UNSUPPORTED_VERSION],
    ids=["crc-failure", "encrypted-flag", "unknown-compression", "unsupported-version"],
)
def test_an_unreadable_voter_csv_is_corrupt_not_missing(payload: bytes) -> None:
    with pytest.raises(EdStatewideReportCorruptError):
        read_ed_statewide_voter_csv(payload)


@pytest.mark.parametrize(
    "response",
    [
        _envelope(_corrupt_ed_zip()),
        _envelope(ENCRYPTED_FLAG),
        _envelope(UNKNOWN_COMPRESSION),
        _envelope(UNSUPPORTED_VERSION),
        httpx.Response(200, text="<html>busy</html>"),
        httpx.Response(200, json={"message": "busy"}),
        httpx.Response(200, json=["busy"]),
        _envelope(_zip_of_voter_csv(f"{ED_HEADER}\n".encode() + b"\xff\xfe,x,y,z,1\n")),
        _envelope(_zip_of_voter_csv(f"{ED_HEADER}\n{'A' * 200_000},x,y,z,1\n".encode())),
    ],
    ids=[
        "crc-failure",
        "encrypted-flag",
        "unknown-compression",
        "unsupported-version",
        "html-body",
        "no-upload-key",
        "json-not-an-object",
        "csv-not-utf8",
        "csv-field-over-limit",
    ],
)
@respx.mock
def test_an_unreadable_report_is_a_failed_day_and_the_batch_finishes(
    response: httpx.Response,
) -> None:
    _mock_early(respx.mock)
    _mock_election_day(respx.mock, response)
    store = _KeepingStore()

    result = _backfill(store)

    assert result.failed_dates == ("2026-11-03",)
    assert result.skipped_empty_dates == ()
    assert result.days_wrote == 1


@pytest.mark.parametrize(
    "response",
    [httpx.Response(404), httpx.Response(200, content=b"")],
    ids=["http-404", "empty-body"],
)
@respx.mock
def test_a_stored_day_whose_report_comes_back_empty_stays_published(
    response: httpx.Response,
) -> None:
    _mock_early(respx.mock)
    _mock_election_day(respx.mock, response)
    store = _KeepingStore()
    store.day_hashes[(DEFAULT_NOV_2026_GENERAL_ID, "2026-11-03")] = "stored-election-day"

    result = _backfill(store)

    assert result.skipped_empty_dates == ()
    assert result.failed_dates == ("2026-11-03",)
    assert result.days[-1].sha256 == "stored-election-day"
    assert store.published_ev_date == "2026-11-03"


@respx.mock
def test_any_other_http_failure_stays_failed() -> None:
    _mock_early(respx.mock)
    _mock_election_day(respx.mock, httpx.Response(500))
    store = _KeepingStore()

    result = _backfill(store)

    assert result.failed_dates == ("2026-11-03",)
    assert result.skipped_empty_dates == ()
    assert result.days_wrote == 1


def test_every_store_requires_a_vote_period() -> None:
    store: Any = EmptyHashStore()
    unbound: Any = CloudflareEarlyVoteStore.write_database
    with pytest.raises(TypeError, match="vote_period"):
        store.write_database("53815", b"x", "d", ev_date_iso="2026-10-14")
    with pytest.raises(TypeError, match="vote_period"):
        unbound(None, "53815", b"x", "d")


OLD_DAY_TABLE = (
    "CREATE TABLE early_vote_day (source_election_id TEXT NOT NULL, ev_date TEXT NOT NULL, "
    "sha256 TEXT NOT NULL, file_id TEXT NOT NULL, PRIMARY KEY (source_election_id, ev_date))"
)


def _columns(conn: sqlite3.Connection) -> list[str]:
    return [row[0] for row in conn.execute(DAY_COLUMNS_SQL)]


def test_no_table_means_nothing_to_alter() -> None:
    conn = sqlite3.connect(":memory:")
    assert _columns(conn) == []
    assert day_migration_statements(_columns(conn)) == []


def test_an_old_table_gains_the_column_and_its_rows_read_early() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(OLD_DAY_TABLE)
    conn.execute("INSERT INTO early_vote_day VALUES ('53815', '2026-10-14', 's', 'f')")

    statements = day_migration_statements(_columns(conn))
    assert len(statements) == 1
    conn.execute(statements[0])

    assert conn.execute("SELECT vote_period FROM early_vote_day").fetchall() == [("early",)]
    assert day_migration_statements(_columns(conn)) == []


def test_a_current_table_needs_no_alter() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(_DAY_TABLE_SQL)

    assert "vote_period" in _columns(conn)
    assert day_migration_statements(_columns(conn)) == []


class _SqliteStore(CloudflareEarlyVoteStore):
    def __init__(self, conn: sqlite3.Connection, client: httpx.Client) -> None:
        super().__init__(
            account_id="a",
            api_token="t",
            kv_namespace_id="n",
            client=client,
        )
        self.conn = conn

    @override
    def _query_rows(self, database_id: str, sql: str, params: list[str]) -> list[dict[str, Any]]:
        cur = self.conn.execute(sql, params)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]

    @override
    def _statements(self, database_id: str, statements: list[tuple[str, list[str]]]) -> None:
        for sql, params in statements:
            self.conn.execute(sql, params)


@pytest.mark.parametrize("existing", [None, OLD_DAY_TABLE], ids=["new-database", "old-table"])
def test_ensure_schema_leaves_a_table_with_the_column_and_can_repeat(existing: str | None) -> None:
    conn = sqlite3.connect(":memory:")
    if existing:
        conn.execute(existing)
        conn.execute("INSERT INTO early_vote_day VALUES ('53815', '2026-10-14', 's', 'f')")

    with httpx.Client(base_url="https://api.cloudflare.com/client/v4", timeout=30.0) as client:
        _SqliteStore(conn, client)._ensure_schema("db")
        _SqliteStore(conn, client)._ensure_schema("db")

    assert "vote_period" in _columns(conn)
    if existing:
        assert conn.execute("SELECT vote_period FROM early_vote_day").fetchall() == [("early",)]


def test_the_upsert_writes_vote_period_on_insert_and_on_conflict() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(_DAY_TABLE_SQL)

    conn.execute(_DAY_UPSERT_SQL, ("53815", "2026-11-03", "s1", "f1", "election_day"))
    inserted = conn.execute("SELECT vote_period FROM early_vote_day").fetchall()
    conn.execute(_DAY_UPSERT_SQL, ("53815", "2026-11-03", "s2", "f2", "early"))
    updated = conn.execute("SELECT sha256, vote_period FROM early_vote_day").fetchall()

    assert inserted == [("election_day",)]
    assert updated == [("s2", "early")]


def _with_election_day(db: Any, ev_date: str) -> Any:
    db._conn.execute(
        "UPDATE early_vote_day SET vote_period = 'election_day' WHERE ev_date = ?", (ev_date,)
    )
    return db


@pytest.mark.asyncio
async def test_by_day_entries_carry_vote_period() -> None:
    db = _with_election_day(_db(DAYS), "2026-10-20")

    body = await _get(db, "by-day")

    assert [(e["ev_date"], e["vote_period"]) for e in body["series"]] == [
        ("2026-10-19", "early"),
        ("2026-10-20", "election_day"),
    ]
    assert body["series"][1]["cumulative_total"] == 48


@pytest.mark.asyncio
async def test_county_by_day_entries_carry_vote_period() -> None:
    db = _with_election_day(_db(DAYS), "2026-10-20")

    body = await _get(db, "county-by-day")

    assert [e["vote_period"] for e in body["series"]] == ["early", "election_day"]


@pytest.mark.asyncio
@pytest.mark.parametrize("resource", ["totals", "county", "by-method"])
async def test_a_one_day_body_names_its_vote_period(resource: str) -> None:
    db = _with_election_day(_db(DAYS), "2026-10-20")

    election_day = await _get(db, resource, ev_date="2026-10-20")
    early = await _get(db, resource, ev_date="2026-10-19")

    assert election_day["vote_period"] == "election_day"
    assert early["vote_period"] == "early"


@pytest.mark.asyncio
async def test_a_cumulative_body_keeps_its_shape() -> None:
    body = await _get(_db(DAYS), "totals")

    assert "vote_period" not in body
    assert body["days"] == ["2026-10-19", "2026-10-20"]


class _BrokenDayList:
    """A D1 that rejects the day-list query, as a database without ``vote_period`` does."""

    def __init__(self, db: Any) -> None:
        self._db = db

    def prepare(self, sql: str) -> Any:
        if sql == _DAY_LIST_SQL:
            raise sqlite3.OperationalError("no such column: vote_period")
        return self._db.prepare(sql)


@pytest.mark.asyncio
async def test_a_failed_day_list_fails_the_request_instead_of_serving_one_day() -> None:
    with pytest.raises(sqlite3.OperationalError):
        await build_precomputed_aggregate_resource(
            cast(Any, _BrokenDayList(_db(DAYS))), "53815", "totals", kv_sha256=KV
        )


def test_elections_list_each_day_with_its_vote_period(monkeypatch: pytest.MonkeyPatch) -> None:
    from ev_api.app import ReadDeps, create_app

    from texas_turnout_scraper import early_vote_sync

    monkeypatch.setattr(early_vote_sync, "CHUNK_RAW_BYTES", 16)
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    for sql in (*_SCHEMA_CREATES, *PRECOMPUTE_SCHEMA_CREATES):
        conn.execute(sql)
    _store_day(conn, "2026-10-19", DAY1)
    _store_day(conn, "2026-10-20", DAY2)
    conn.execute(
        "UPDATE early_vote_day SET vote_period = 'election_day' WHERE ev_date = '2026-10-20'"
    )
    client = TestClient(
        create_app(
            ReadDeps(kv=_Kv(), store=cast(Any, None), rosters=_Rosters({"53815": _Db(conn)}))
        )
    )

    detail = client.get("/elections/53815", headers=AGGREGATES).json()
    listed = client.get("/elections", headers=AGGREGATES).json()["elections"][0]

    assert [(d["ev_date"], d["vote_period"]) for d in detail["days"]] == [
        ("2026-10-19", "early"),
        ("2026-10-20", "election_day"),
    ]
    assert listed["days"] == detail["days"]
    assert detail["last_ev_date"] == "2026-10-20"
    assert detail["total_rows"] == 3


def _partition_hits(vuids: list[str]) -> dict[str, list[int]]:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE early_vote_roster_row (id_voter TEXT, county TEXT, voting_method TEXT, "
        "day INTEGER, removed_in INTEGER)"
    )
    conn.executemany(
        "INSERT INTO early_vote_roster_row VALUES (?, ?, 'IN-PERSON', 1, NULL)",
        [(vuid, vuid) for vuid in vuids],
    )
    hits: dict[str, list[int]] = {vuid: [] for vuid in vuids}
    for index, (sql, params) in enumerate(PARTITIONS):
        for _day, county, _method, n in conn.execute(sql, params):
            assert n == 1
            hits[county].append(index)
    return hits


def test_there_are_a_hundred_two_digit_partitions_and_a_catch_all() -> None:
    assert len(PARTITIONS) == 101


def test_every_vuid_lands_in_exactly_one_partition() -> None:
    every_pair = [f"00000000{n:02d}" for n in range(100)]
    odd = ["0", "7", "42", "100", "ABCDEF1", "1234567X", "12345678AB", "ab", "a-", "9999999999"]
    vuids = [*every_pair, *odd, "1234567890", "9876543210"]

    hits = _partition_hits([vuid.upper() for vuid in vuids])

    assert all(len(found) == 1 for found in hits.values()), {
        v: f for v, f in hits.items() if len(f) != 1
    }
    for n in range(100):
        assert hits[f"00000000{n:02d}"] == [n]
    for vuid in ("0", "7", "ABCDEF1", "1234567X", "12345678AB", "AB", "A-"):
        assert hits[vuid] == [100]


def test_a_voters_rows_stay_in_one_partition() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE early_vote_roster_row (id_voter TEXT, county TEXT, voting_method TEXT, "
        "day INTEGER, removed_in INTEGER)"
    )
    conn.executemany(
        "INSERT INTO early_vote_roster_row VALUES ('0000000042', 'TRAVIS', 'IN-PERSON', ?, NULL)",
        [(1,), (2,), (3,)],
    )
    counts = [sum(n for *_, n in conn.execute(sql, params)) for sql, params in PARTITIONS]
    assert sum(counts) == 1
    assert counts[42] == 1
