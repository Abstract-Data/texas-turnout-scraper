"""An unchanged early-vote day costs one download, one parse, and no repeated schema DDL."""

from __future__ import annotations

import base64
import logging
import sqlite3
from collections import Counter
from datetime import date
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from typing_extensions import override

from texas_turnout_scraper import early_vote_roster, early_vote_sync, r2_snapshots
from texas_turnout_scraper.civix.envelope import _decode_envelope
from texas_turnout_scraper.early_vote_backfill import (
    format_backfill_report,
    plan_backfill_load_dates,
    run_backfill,
)
from texas_turnout_scraper.early_vote_sync import (
    _SCHEMA_CREATES,
    CLOUDFLARE_API_BASE,
    CloudflareEarlyVoteStore,
    EmptyHashStore,
    prepare_payload_bytes,
    sync_early_vote,
)

ELECTION = "53815"
TODAY = date(2026, 10, 20)
HEADER = b"COUNTY,VOTER_NAME,ID_VOTER,VOTING_METHOD,PRECINCT\n"
R2_VARS = (
    "CLOUDFLARE_ACCOUNT_ID",
    "EV_SNAPSHOT_BUCKET",
    "EV_SNAPSHOT_R2_ACCESS_KEY_ID",
    "EV_SNAPSHOT_R2_SECRET_ACCESS_KEY",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
)


def _roster(rows: int) -> bytes:
    body = b"".join(
        b"TEST,SYNTHETIC VOTER,%010d,IN-PERSON,1\n" % (index + 1) for index in range(rows)
    )
    return HEADER + body


def _response(payload: bytes) -> httpx.Response:
    return httpx.Response(200, json={"upload": base64.b64encode(payload).decode("ascii")})


class _DayStore:
    def __init__(self) -> None:
        self.day_hashes: dict[str, str] = {}
        self.writes: list[str] = []

    def stored_hash(self, election_id: str, *, ev_date_iso: str | None = None) -> str | None:
        return self.day_hashes.get(ev_date_iso or "")

    def write_database(
        self,
        election_id: str,
        decoded: bytes,
        digest: str,
        *,
        ev_date_iso: str | None = None,
        update_pointer: bool = False,
    ) -> None:
        assert ev_date_iso
        self.writes.append(ev_date_iso)
        self.day_hashes[ev_date_iso] = digest

    def bump_version(self, election_id: str, digest: str) -> str:
        return "1"

    def finalize_published_roster(
        self,
        election_id: str,
        ev_date_iso: str,
        content_digest: str,
        *,
        manifest_sha256: str | None = None,
    ) -> str:
        return "1"


class _Counts:
    """Spies on the portal pull and on both CSV parse entry points."""

    def __init__(self, payloads: dict[date, bytes]) -> None:
        self.payloads = payloads
        self.pulls: Counter[date] = Counter()
        self.prepares = 0
        self.canonicalizes = 0

    def pull(self, _election_id: str, ev_day: date, **_: object) -> httpx.Response:
        self.pulls[ev_day] += 1
        return _response(self.payloads[ev_day])

    def prepare(self, decoded: bytes, kind: Any) -> Any:
        self.prepares += 1
        return _REAL_PREPARE(decoded, kind)

    def canonicalize(self, decoded: bytes) -> Any:
        self.canonicalizes += 1
        return _REAL_CANONICALIZE(decoded)


_REAL_PREPARE = early_vote_sync.prepare_payload_bytes
_REAL_CANONICALIZE = early_vote_roster.canonicalize_roster_csv


def _backfill(
    counts: _Counts,
    store: Any,
    calendar: tuple[date, ...],
    *,
    dry_run: bool = False,
) -> Any:
    with (
        patch(
            "texas_turnout_scraper.early_vote_backfill.pull_statewide_roster_file",
            side_effect=counts.pull,
        ),
        patch(
            "texas_turnout_scraper.early_vote_backfill.prepare_payload_bytes",
            side_effect=counts.prepare,
        ),
        patch(
            "texas_turnout_scraper.early_vote_sync.prepare_payload_bytes",
            side_effect=counts.prepare,
        ),
        patch(
            "texas_turnout_scraper.early_vote_sync.canonicalize_roster_csv",
            side_effect=counts.canonicalize,
        ),
        patch(
            "texas_turnout_scraper.early_vote_roster.canonicalize_roster_csv",
            side_effect=counts.canonicalize,
        ),
    ):
        plan = plan_backfill_load_dates(ELECTION, calendar, today=TODAY)
        return run_backfill(
            ELECTION,
            store,
            plan,
            dry_run=dry_run,
            payload_kind="statewide_roster",
            defer_kv_bump=True,
        )


PAST = (date(2026, 10, 14), date(2026, 10, 15), date(2026, 10, 16))


def test_each_past_day_is_pulled_once_and_parsed_once_changed_or_unchanged() -> None:
    payloads = {day: _roster(index + 1) for index, day in enumerate(PAST)}
    store = _DayStore()

    first = _Counts(payloads)
    wrote = _backfill(first, store, PAST)
    assert wrote.days_wrote == 3
    assert first.pulls == Counter(dict.fromkeys(PAST, 1))
    assert first.prepares == 3
    assert first.canonicalizes == 3

    again = _Counts(payloads)
    unchanged = _backfill(again, store, PAST)
    assert unchanged.days_unchanged == 3
    assert unchanged.days_wrote == 0
    assert again.pulls == Counter(dict.fromkeys(PAST, 1))
    assert again.prepares == 3
    assert again.canonicalizes == 3
    assert unchanged.manifest_sha256 == wrote.manifest_sha256
    assert store.writes == [day.isoformat() for day in PAST]


def test_an_empty_day_is_skipped_and_counted_and_a_future_day_is_never_pulled() -> None:
    future = date(2026, 10, 21)
    calendar = (*PAST, future)
    payloads = {PAST[0]: _roster(2), PAST[1]: HEADER, PAST[2]: _roster(1)}
    counts = _Counts(payloads)
    store = _DayStore()

    result = _backfill(counts, store, calendar)

    assert future not in counts.pulls
    assert counts.pulls == Counter(dict.fromkeys(PAST, 1))
    assert store.writes == ["2026-10-14", "2026-10-16"]
    assert result.days_skipped_empty == 1
    assert result.days_wrote == 2
    report = format_backfill_report(result).splitlines()
    assert "skipped_empty_count=1" in report
    assert "skipped_empty_dates=2026-10-15" in report
    assert "skipped_future_count=1" in report
    assert "skipped_future_dates=2026-10-21" in report
    assert "load_plan_count=2" in report
    assert "load_plan_dates=2026-10-14,2026-10-16" in report
    assert "ev_days_total=2" in report


def test_an_empty_day_in_a_dry_run_is_skipped_without_a_write() -> None:
    payloads = {PAST[0]: HEADER, PAST[1]: _roster(1), PAST[2]: _roster(2)}
    counts = _Counts(payloads)
    store = EmptyHashStore()
    result = _backfill(counts, store, PAST, dry_run=True)
    assert result.days_dry_run == 2
    assert result.days_skipped_empty == 1
    assert counts.prepares == 3


def test_prepared_bytes_give_the_same_digest_without_a_second_parse() -> None:
    response = _response(_roster(3).replace(b"\n", b"\r\n"))
    plain = sync_early_vote(
        ELECTION,
        response,
        _DayStore(),
        dry_run=True,
        payload_kind="statewide_roster",
        ev_date_iso="2026-10-14",
    )
    prepared = prepare_payload_bytes(_decode_envelope(response), "statewide_roster")
    counts = _Counts({})
    with patch(
        "texas_turnout_scraper.early_vote_sync.prepare_payload_bytes",
        side_effect=counts.prepare,
    ):
        shortcut = sync_early_vote(
            ELECTION,
            response,
            _DayStore(),
            dry_run=True,
            payload_kind="statewide_roster",
            ev_date_iso="2026-10-14",
            prepared=prepared,
        )
    assert counts.prepares == 0
    assert shortcut.sha256 == plain.sha256
    assert shortcut.decoded_bytes == plain.decoded_bytes
    assert shortcut.plan is not None and plain.plan is not None
    assert shortcut.plan.source_rows == plain.plan.source_rows == 3


class _CloudflareFake:
    """The Cloudflare database list/create API; D1 queries are answered by sqlite."""

    def __init__(self, *, exists: bool) -> None:
        self.databases: list[dict[str, str]] = []
        if exists:
            self.databases.append({"name": early_vote_sync.database_name(ELECTION), "uuid": "db-1"})
        self.lists = 0
        self.creates = 0

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/d1/database") and request.method == "GET":
            self.lists += 1
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "result": self.databases,
                    "result_info": {"total_count": len(self.databases)},
                },
            )
        if request.url.path.endswith("/d1/database") and request.method == "POST":
            self.creates += 1
            uuid = f"db-created-{self.creates}"
            self.databases.append({"name": early_vote_sync.database_name(ELECTION), "uuid": uuid})
            return httpx.Response(200, json={"success": True, "result": {"uuid": uuid}})
        return httpx.Response(500, json={"success": False})


class _SqliteStore(CloudflareEarlyVoteStore):
    def __init__(self, cloudflare: _CloudflareFake, conn: sqlite3.Connection) -> None:
        super().__init__(
            account_id="a",
            api_token="t",
            kv_namespace_id="n",
            client=httpx.Client(
                base_url=CLOUDFLARE_API_BASE, transport=httpx.MockTransport(cloudflare.handle)
            ),
        )
        self.conn = conn
        self.schema_batches = 0
        self.database_ids: set[str] = set()

    @override
    def _query_rows(self, database_id: str, sql: str, params: list[str]) -> list[dict[str, Any]]:
        self.database_ids.add(database_id)
        cur = self.conn.execute(sql, params)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]

    @override
    def _statements(self, database_id: str, statements: list[tuple[str, list[str]]]) -> None:
        self.database_ids.add(database_id)
        if [sql for sql, _ in statements] == list(_SCHEMA_CREATES):
            self.schema_batches += 1
        for sql, params in statements:
            self.conn.execute(sql, params)


ELEVEN = tuple(date(2026, 10, 9 + offset) for offset in range(11))


def _eleven_days(store: _SqliteStore) -> Any:
    counts = _Counts({day: _roster(1 + index % 3) for index, day in enumerate(ELEVEN)})
    return _backfill(counts, store, ELEVEN)


@pytest.fixture
def _no_r2(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in R2_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.mark.usefixtures("_no_r2")
def test_eleven_days_list_databases_and_run_the_schema_batch_once_per_store() -> None:
    conn = sqlite3.connect(":memory:")
    first_fake = _CloudflareFake(exists=True)
    first = _SqliteStore(first_fake, conn)
    wrote = _eleven_days(first)
    assert wrote.days_wrote == 11
    assert first_fake.lists == 1
    assert first.schema_batches == 1

    second_fake = _CloudflareFake(exists=True)
    second = _SqliteStore(second_fake, conn)
    unchanged = _eleven_days(second)
    assert unchanged.days_unchanged == 11
    assert second_fake.lists == 1
    assert second.schema_batches == 1


@pytest.mark.usefixtures("_no_r2")
def test_a_database_created_mid_run_is_used_by_every_later_day() -> None:
    conn = sqlite3.connect(":memory:")
    fake = _CloudflareFake(exists=False)
    store = _SqliteStore(fake, conn)
    result = _eleven_days(store)
    assert result.days_wrote == 11
    assert fake.creates == 1
    assert fake.lists == 2
    assert store.schema_batches == 1
    assert store.database_ids == {"db-created-1"}
    rows = conn.execute("SELECT COUNT(*) FROM early_vote_day").fetchone()
    assert rows == (11,)


@pytest.mark.usefixtures("_no_r2")
def test_missing_r2_settings_warn_once_by_name_in_apply_mode(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(r2_snapshots, "_missing_env_warned", False, raising=False)
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct-sentinel-value")
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "key-sentinel-value")
    with caplog.at_level(logging.INFO):
        _eleven_days(_SqliteStore(_CloudflareFake(exists=True), sqlite3.connect(":memory:")))
    warnings = [
        r for r in caplog.records if r.levelno == logging.WARNING and "R2" in r.getMessage()
    ]
    assert len(warnings) == 1
    text = warnings[0].getMessage()
    assert "EV_SNAPSHOT_BUCKET" in text
    assert "R2_SECRET_ACCESS_KEY" in text
    assert "CLOUDFLARE_ACCOUNT_ID" not in text
    assert "sentinel" not in caplog.text


@pytest.mark.usefixtures("_no_r2")
def test_missing_r2_settings_are_silent_in_a_dry_run(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(r2_snapshots, "_missing_env_warned", False, raising=False)
    counts = _Counts({day: _roster(1) for day in PAST})
    with caplog.at_level(logging.INFO):
        _backfill(counts, EmptyHashStore(), PAST, dry_run=True)
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
