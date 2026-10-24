"""Early-vote hash gate, unchanged exit, and the 10 GB D1 refusal."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import tempfile
from datetime import date
from pathlib import Path
from urllib.parse import unquote

import httpx
import pytest
import respx

from texas_turnout_scraper.civix.evr import API_PREFIX, BASE_URL
from texas_turnout_scraper.early_vote_sync import (
    _CHUNK_INSERT_SQL,
    _DAY_FILE_IDS_SQL,
    _DELETE_CHUNK_SQL,
    _DELETE_FILE_SQL,
    _FILE_ID_FOR_HASH_SQL,
    _FILE_IDS_SQL,
    _FILE_INSERT_SQL,
    _POINTER_SELECT_SQL,
    _POINTER_UPSERT_SQL,
    CHUNK_RAW_BYTES,
    CLOUDFLARE_API_BASE,
    D1_DATABASE_CAP_BYTES,
    EVR_HTTP_USER_AGENT,
    EVR_RETRY_STATUS_CODES,
    KV_NOT_FOR_ELECTION_NIGHT,
    PEAK_STORED_COPIES,
    CloudflareEarlyVoteStore,
    EarlyVoteCapError,
    EarlyVoteFileView,
    EarlyVoteSyncError,
    PublishPlan,
    _plan_statements,
    assert_within_cap,
    build_publish_plan,
    choose_published_file,
    database_name,
    estimated_stored_bytes,
    format_sync_report,
    hash_key,
    main,
    pull_early_vote_file,
    pull_statewide_roster_file,
    sha256_decoded,
    stale_file_ids,
    sync_early_vote,
    version_key,
)

FIXTURE = Path(__file__).parent.parent / "fixtures" / "early_vote" / "sample_envelope.json"
STATEWIDE_FIXTURE = (
    Path(__file__).parent.parent / "fixtures" / "early_vote" / "statewide_roster_envelope.json"
)
ACCOUNT = "00000000000000000000000000000001"
NAMESPACE = "ns-test"


class RecordingStore:
    def __init__(self, stored: str | None = None, *, fail_write: bool = False) -> None:
        self.stored = stored
        self.fail_write = fail_write
        self.calls: list[str] = []
        self.written: bytes | None = None
        self.version: str | None = None
        self.day_hashes: dict[tuple[str, str], str] = {}
        self.bump_count = 0

    def stored_hash(self, election_id: str, *, ev_date_iso: str | None = None) -> str | None:
        self.calls.append(f"hash:{election_id}:{ev_date_iso or ''}")
        if ev_date_iso:
            return self.day_hashes.get((election_id, ev_date_iso))
        return self.stored

    def write_database(
        self,
        election_id: str,
        decoded: bytes,
        digest: str,
        *,
        ev_date_iso: str | None = None,
        update_pointer: bool = False,
    ) -> None:
        self.calls.append(f"write:{election_id}:{ev_date_iso or ''}")
        if self.fail_write:
            raise EarlyVoteSyncError("D1 write failed")
        self.written = decoded
        if ev_date_iso:
            self.day_hashes[(election_id, ev_date_iso)] = digest
        if update_pointer:
            self.calls.append(f"pointer:{election_id}")

    def bump_version(self, election_id: str, digest: str) -> str:
        self.calls.append(f"bump:{election_id}")
        self.bump_count += 1
        self.version = "2"
        self.stored = digest
        return self.version

    def finalize_published_roster(
        self,
        election_id: str,
        ev_date_iso: str,
        content_digest: str,
        *,
        manifest_sha256: str | None = None,
    ) -> str:
        self.calls.append(f"finalize:{election_id}:{ev_date_iso}")
        return self.bump_version(election_id, content_digest)


def _envelope(payload: bytes) -> httpx.Response:
    encoded = base64.b64encode(payload).decode("ascii")
    return httpx.Response(200, json={"upload": encoded})


def test_hash_is_sha256_of_decoded_bytes_not_the_envelope() -> None:
    payload = b'{"kind":"evr-early-vote","rows":0}'
    response = _envelope(payload)
    store = RecordingStore()

    result = sync_early_vote("53813", response, store, dry_run=True)

    assert result.sha256 == sha256_decoded(payload)
    assert result.sha256 == hashlib.sha256(payload).hexdigest()
    assert result.sha256 != hashlib.sha256(response.content).hexdigest()
    assert result.election_id == "53813"
    assert result.decoded_bytes == len(payload)


def test_leading_zero_election_id_stays_a_string() -> None:
    store = RecordingStore()
    result = sync_early_vote("0123", _envelope(b"rows"), store, dry_run=True)

    assert result.election_id == "0123"
    assert database_name("0123") == "ev-0123"
    assert version_key("0123") == "ev:0123:version"
    assert hash_key("0123") == "ev:0123:sha256"
    assert store.calls == ["hash:0123:"]


def test_unchanged_hash_exits_without_write() -> None:
    payload = b"same-file"
    digest = sha256_decoded(payload)
    store = RecordingStore(stored=digest)

    result = sync_early_vote("53813", _envelope(payload), store)

    assert result.status == "unchanged"
    assert result.exit_code == 0
    assert result.version is None
    assert store.calls == ["hash:53813:"]
    assert store.written is None


def test_new_hash_writes_d1_before_kv_bump() -> None:
    payload = b"new-file"
    store = RecordingStore()

    result = sync_early_vote("53813", _envelope(payload), store)

    assert result.status == "wrote"
    assert result.version == "2"
    assert store.calls == ["hash:53813:", "write:53813:", "bump:53813"]
    assert store.written == payload


def test_failed_d1_write_does_not_bump_kv() -> None:
    store = RecordingStore(fail_write=True)

    with pytest.raises(EarlyVoteSyncError, match="D1 write failed"):
        sync_early_vote("53813", _envelope(b"new-file"), store)

    assert store.calls == ["hash:53813:", "write:53813:"]
    assert store.version is None


def test_over_cap_refuses_before_create_or_kv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("texas_turnout_scraper.early_vote_sync.D1_DATABASE_CAP_BYTES", 1)
    payload = b"VOTER_NAME=Ada ID_VOTER=0123456789"
    store = RecordingStore()

    with pytest.raises(EarlyVoteCapError, match="election 53813 is") as raised:
        sync_early_vote(
            "53813",
            _envelope(payload),
            store,
            dry_run=False,
        )

    assert "Ada" not in str(raised.value)
    assert "0123456789" not in str(raised.value)
    assert store.calls == ["hash:53813:"]
    assert store.written is None
    assert store.version is None


def test_over_cap_uses_the_real_limit_constant(monkeypatch: pytest.MonkeyPatch) -> None:
    assert D1_DATABASE_CAP_BYTES == 10_000_000_000
    assert PEAK_STORED_COPIES == 3
    fitting = (D1_DATABASE_CAP_BYTES // (4 * PEAK_STORED_COPIES)) * 3
    assert estimated_stored_bytes(fitting) <= D1_DATABASE_CAP_BYTES
    assert_within_cap(fitting, "53813")
    with pytest.raises(EarlyVoteCapError, match=f"{fitting + 1} bytes"):
        assert_within_cap(fitting + 1, "53813")
    with pytest.raises(EarlyVoteCapError, match="retained copies"):
        assert_within_cap(D1_DATABASE_CAP_BYTES, "53813")

    monkeypatch.setattr(
        "texas_turnout_scraper.early_vote_sync.D1_DATABASE_CAP_BYTES",
        4,
    )
    store = RecordingStore()
    with pytest.raises(EarlyVoteCapError):
        sync_early_vote("53813", _envelope(b"12345"), store)
    assert store.calls == ["hash:53813:"]


def test_unchanged_file_exits_even_when_over_the_patched_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"12345"
    store = RecordingStore(stored=sha256_decoded(payload))
    monkeypatch.setattr(
        "texas_turnout_scraper.early_vote_sync.D1_DATABASE_CAP_BYTES",
        4,
    )

    result = sync_early_vote("53813", _envelope(payload), store)

    assert result.status == "unchanged"
    assert result.exit_code == 0
    assert store.calls == ["hash:53813:"]


def test_dry_run_report_has_no_file_body(caplog: pytest.LogCaptureFixture) -> None:
    payload = b"VOTER_NAME=Ada ID_VOTER=0123456789"
    store = RecordingStore()
    with caplog.at_level(logging.INFO):
        result = sync_early_vote("53813", _envelope(payload), store, dry_run=True)

    report = format_sync_report(result)
    assert result.status == "dry_run"
    assert "Ada" not in report
    assert "0123456789" not in report
    assert "Ada" not in caplog.text
    assert "0123456789" not in caplog.text
    assert KV_NOT_FOR_ELECTION_NIGHT in report
    assert "d1_database=ev-53813" in report
    assert "kv_version_key=ev:53813:version" in report
    assert store.calls == ["hash:53813:"]


def test_invalid_election_id_is_rejected_before_a_write() -> None:
    store = RecordingStore()
    with pytest.raises(ValueError, match="election id"):
        sync_early_vote("0", _envelope(b"x"), store)
    assert store.calls == []


def test_statewide_roster_dry_run_reports_columns_not_rows(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(
        [
            "--election-id",
            "53815",
            "--envelope-file",
            str(STATEWIDE_FIXTURE),
            "--payload-kind",
            "statewide_roster",
            "--dry-run",
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "payload_kind=statewide_roster" in captured.out
    assert (
        "csv_columns=COUNTY,VOTER_NAME,ID_VOTER,VOTING_METHOD,PRECINCT,POLL_PLACE_NAME"
        in captured.out
    )
    assert "dropped_columns" not in captured.out
    assert "voting_method_counts=IN-PERSON:1,MAIL-IN:2" in captured.out
    assert "source_rows=3" in captured.out
    assert "SMITH" not in captured.out
    assert "1000000001" not in captured.out


def test_pull_statewide_retries_503_and_sets_user_agent() -> None:
    payload = b"COUNTY,VOTER_NAME,ID_VOTER,VOTING_METHOD,PRECINCT\n"
    encoded = base64.b64encode(payload).decode("ascii")
    with respx.mock:
        route = respx.get(f"{BASE_URL}{API_PREFIX}/getFile").mock(
            side_effect=[
                httpx.Response(503),
                httpx.Response(200, json={"upload": encoded}),
            ]
        )
        response = pull_statewide_roster_file("53815", date(2026, 10, 20), http_backend="httpx")
        assert route.call_count == 2
        assert "Abstract-Data" in route.calls[0].request.headers.get("User-Agent", "")
        assert response.json()["upload"] == encoded
    assert 503 in EVR_RETRY_STATUS_CODES
    assert "Abstract-Data" in EVR_HTTP_USER_AGENT


def test_pull_sends_election_id_as_a_string() -> None:
    payload = b'{"rows":0}'
    encoded = base64.b64encode(payload).decode("ascii")
    with respx.mock:
        route = respx.get(f"{BASE_URL}{API_PREFIX}/getFile").mock(
            return_value=httpx.Response(200, json={"upload": encoded})
        )
        with httpx.Client() as client:
            response = pull_early_vote_file("0123", date(2026, 2, 27), client=client)

    assert route.called
    request = route.calls[0].request
    assert request.url.params["electionId"] == "0123"
    assert request.url.params["electionDate"] == "02/27/2026"
    assert request.url.params["type"] == "EVR_EARLYVOTING"
    assert response.json()["upload"] == encoded


def _cf_store(client: httpx.Client) -> CloudflareEarlyVoteStore:
    return CloudflareEarlyVoteStore(
        account_id=ACCOUNT,
        api_token="token-not-logged",
        kv_namespace_id=NAMESPACE,
        client=client,
    )


def _ok_query() -> httpx.Response:
    return httpx.Response(200, json={"success": True, "result": [{"success": True}]})


def test_cloudflare_store_creates_one_database_then_bumps_kv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("texas_turnout_scraper.early_vote_sync.CHUNK_RAW_BYTES", 3)
    payload = b"abcdef"
    digest = sha256_decoded(payload)
    list_url = f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT}/d1/database"
    with respx.mock:
        respx.get(list_url).mock(
            return_value=httpx.Response(
                200,
                json={"success": True, "result": [], "result_info": {"total_count": 0}},
            )
        )
        create = respx.post(list_url).mock(
            return_value=httpx.Response(
                200,
                json={"success": True, "result": {"uuid": "db-1", "name": "ev-53813"}},
            )
        )
        query = respx.post(f"{list_url}/db-1/query").mock(return_value=_ok_query())
        respx.get(url__regex=r".*/storage/kv/.*").mock(return_value=httpx.Response(404))
        puts = respx.put(url__regex=r".*/storage/kv/.*").mock(
            return_value=httpx.Response(200, json={"success": True})
        )
        with httpx.Client(base_url=CLOUDFLARE_API_BASE) as client:
            store = _cf_store(client)
            result = sync_early_vote("53813", _envelope(payload), store)
        call_kinds = [
            "query" if "query" in str(call.request.url) else call.request.method
            for call in respx.calls
        ]

    assert result.status == "wrote"
    assert result.version == "1"
    assert create.call_count == 1
    assert json.loads(create.calls[0].request.content) == {"name": "ev-53813"}
    assert query.call_count >= 1
    put_urls = [str(call.request.url) for call in puts.calls]
    assert any("ev%3A53813%3Aversion" in url for url in put_urls)
    assert any("ev%3A53813%3Asha256" in url for url in put_urls)
    version_call = next(call for call in puts.calls if "version" in str(call.request.url))
    hash_call = next(call for call in puts.calls if "sha256" in str(call.request.url))
    assert version_call.request.content == b"1"
    assert hash_call.request.content == digest.encode()
    assert call_kinds.index("query") < call_kinds.index("PUT")
    assert CHUNK_RAW_BYTES == 32_768


def test_cloudflare_store_reuses_the_existing_database() -> None:
    list_url = f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT}/d1/database"
    with respx.mock:
        respx.get(list_url).mock(
            return_value=httpx.Response(
                200,
                json={
                    "success": True,
                    "result": [{"name": "ev-53813", "uuid": "db-existing"}],
                    "result_info": {"total_count": 1},
                },
            )
        )
        create = respx.post(list_url).mock(
            return_value=httpx.Response(500, json={"success": False, "errors": []})
        )
        respx.post(f"{list_url}/db-existing/query").mock(return_value=_ok_query())
        respx.get(url__regex=r".*/storage/kv/.*").mock(return_value=httpx.Response(404))
        respx.put(url__regex=r".*/storage/kv/.*").mock(
            return_value=httpx.Response(200, json={"success": True})
        )
        with httpx.Client(base_url=CLOUDFLARE_API_BASE) as client:
            sync_early_vote("53813", _envelope(b"file"), _cf_store(client))

    assert create.call_count == 0


def test_second_database_is_not_created() -> None:
    list_url = f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT}/d1/database"
    with respx.mock:
        respx.get(list_url).mock(
            return_value=httpx.Response(
                200,
                json={
                    "success": True,
                    "result": [
                        {"name": "ev-53813", "uuid": "db-a"},
                        {"name": "ev-53813", "uuid": "db-b"},
                    ],
                    "result_info": {"total_count": 2},
                },
            )
        )
        create = respx.post(list_url).mock(return_value=httpx.Response(500))
        respx.get(url__regex=r".*/storage/kv/.*").mock(return_value=httpx.Response(404))
        puts = respx.put(url__regex=r".*/storage/kv/.*").mock(return_value=httpx.Response(200))
        with httpx.Client(base_url=CLOUDFLARE_API_BASE) as client:
            with pytest.raises(EarlyVoteSyncError, match="refusing to create another"):
                sync_early_vote("53813", _envelope(b"file"), _cf_store(client))

    assert create.call_count == 0
    assert puts.call_count == 0


def test_d1_query_failure_does_not_bump_kv() -> None:
    list_url = f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT}/d1/database"
    with respx.mock:
        respx.get(list_url).mock(
            return_value=httpx.Response(
                200,
                json={
                    "success": True,
                    "result": [{"name": "ev-53813", "uuid": "db-1"}],
                    "result_info": {"total_count": 1},
                },
            )
        )
        respx.post(f"{list_url}/db-1/query").mock(
            return_value=httpx.Response(
                200,
                json={"success": True, "result": [{"success": False}]},
            )
        )
        respx.get(url__regex=r".*/storage/kv/.*").mock(return_value=httpx.Response(404))
        puts = respx.put(url__regex=r".*/storage/kv/.*").mock(return_value=httpx.Response(200))
        with httpx.Client(base_url=CLOUDFLARE_API_BASE) as client:
            with pytest.raises(EarlyVoteSyncError, match="D1 query failed"):
                sync_early_vote("53813", _envelope(b"file"), _cf_store(client))

    assert puts.call_count == 0


def test_dry_run_cli_sample(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["--election-id", "53813", "--envelope-file", str(FIXTURE), "--dry-run"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "election_id=53813" in captured.out
    assert "status=dry_run" in captured.out
    assert "decoded_bytes=" in captured.out
    assert "d1_cap_bytes=10000000000" in captured.out
    assert "within_cap=true" in captured.out
    assert "rows election_id=53813 d1_chunk_rows=1 d1_file_rows=1 d1_rows=2" in captured.out
    assert "d1_statement repeats=1 sql=CREATE TABLE IF NOT EXISTS early_vote_file" in captured.out
    assert "d1_statement repeats=1 sql=INSERT INTO early_vote_chunk" in captured.out
    assert "INSERT INTO early_vote_pointer" in captured.out
    assert (
        "ON CONFLICT(source_election_id) DO UPDATE SET file_id = excluded.file_id" in captured.out
    )
    for line in captured.out.splitlines():
        if "DELETE FROM early_vote_chunk" in line or "DELETE FROM early_vote_file" in line:
            assert "file_id = ?" in line
    assert "kv_version_key=ev:53813:version" in captured.out
    assert KV_NOT_FOR_ELECTION_NIGHT in captured.out
    assert "VOTER_NAME" not in captured.out
    assert "ID_VOTER" not in captured.out
    assert "VOTER_NAME" not in captured.err
    assert "ID_VOTER" not in captured.err


def test_dry_run_plan_omits_bound_file_bytes(capsys: pytest.CaptureFixture[str]) -> None:
    payload = b"VOTER_NAME=Ada ID_VOTER=0123456789"
    encoded = base64.b64encode(payload).decode("ascii")
    with tempfile.NamedTemporaryFile(suffix=".json") as handle:
        handle.write(json.dumps({"upload": encoded}).encode("utf-8"))
        handle.flush()
        exit_code = main(["--election-id", "53813", "--envelope-file", handle.name, "--dry-run"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Ada" not in captured.out
    assert "0123456789" not in captured.out
    assert encoded not in captured.out
    assert "payload_b64" in captured.out
    assert "Ada" not in captured.err
    assert encoded not in captured.err


def test_second_dry_run_skips_the_same_file(capsys: pytest.CaptureFixture[str]) -> None:
    first = main(["--election-id", "53813", "--envelope-file", str(FIXTURE), "--dry-run"])
    first_out = capsys.readouterr().out
    digest = next(
        line.removeprefix("sha256=")
        for line in first_out.splitlines()
        if line.startswith("sha256=")
    )

    second = main(
        [
            "--election-id",
            "53813",
            "--envelope-file",
            str(FIXTURE),
            "--dry-run",
            "--stored-hash",
            digest,
        ]
    )
    second_out = capsys.readouterr().out

    assert first == 0
    assert second == 0
    assert "status=dry_run" in first_out
    assert "d1_statement " in first_out
    assert f"sha256={digest}" in second_out
    assert "status=unchanged" in second_out
    assert "unchanged_skip=true" in second_out
    assert "kv_version_key=ev:53813:version" in second_out
    assert "d1_statement " not in second_out
    assert "VOTER_NAME" not in second_out
    assert "ID_VOTER" not in second_out


def test_dry_run_counts_turnout_rows_for_one_election(capsys: pytest.CaptureFixture[str]) -> None:
    recorded = (
        Path(__file__).parent.parent / "fixtures" / "early_voting" / "civix_earlyvoting_53813.json"
    )
    decoded = recorded.read_bytes()
    with tempfile.NamedTemporaryFile(suffix=".json") as handle:
        handle.write(json.dumps({"upload": base64.b64encode(decoded).decode("ascii")}).encode())
        handle.flush()
        exit_code = main(["--election-id", "53813", "--envelope-file", handle.name, "--dry-run"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "source_rows=3" in captured.out
    assert "d1_aggregate_rows=" in captured.out
    assert "d1_chunk_rows=1 d1_file_rows=1 d1_rows=2" in captured.out
    assert "within_cap=true" in captured.out
    assert "d1_cap_bytes=10000000000" in captured.out
    assert "ANDERSON" not in captured.out
    assert "VOTER_NAME" not in captured.out
    assert "ID_VOTER" not in captured.out


def test_dry_run_does_not_call_cloudflare_when_env_is_set(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", ACCOUNT)
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "token-not-logged")
    monkeypatch.setenv("CLOUDFLARE_KV_NAMESPACE_ID", NAMESPACE)
    with respx.mock:
        route = respx.route(host="api.cloudflare.com").mock(return_value=httpx.Response(500))
        exit_code = main(["--election-id", "53813", "--envelope-file", str(FIXTURE), "--dry-run"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert route.call_count == 0
    assert "status=dry_run" in captured.out
    assert "token-not-logged" not in captured.out
    assert "token-not-logged" not in captured.err


class _MemoryPublish:
    """Applies publish-plan statements so a stopped load can be read back."""

    def __init__(self) -> None:
        self.files: dict[str, tuple[str, int]] = {}
        self.chunks: dict[str, int] = {}
        self.pointer: str | None = None

    def apply(self, sql: str, params: tuple[str, ...]) -> None:
        if sql.startswith("CREATE"):
            return
        if sql == _CHUNK_INSERT_SQL:
            file_id = params[1]
            self.chunks[file_id] = self.chunks.get(file_id, 0) + 1
            return
        if sql == _FILE_INSERT_SQL:
            file_id = params[1]
            self.files[file_id] = (params[2], int(params[4]))
            return
        if sql == _POINTER_UPSERT_SQL:
            self.pointer = params[1]
            return
        if sql == _DELETE_CHUNK_SQL:
            self.chunks.pop(params[1], None)
            return
        if sql == _DELETE_FILE_SQL:
            self.files.pop(params[1], None)
            return
        raise AssertionError(sql)

    def views(self) -> list[EarlyVoteFileView]:
        views: list[EarlyVoteFileView] = []
        for file_id in sorted(set(self.files) | set(self.chunks)):
            digest, count = self.files.get(file_id, ("", 0))
            views.append(
                EarlyVoteFileView(
                    file_id=file_id,
                    sha256=digest,
                    chunk_count=count,
                    chunks_present=self.chunks.get(file_id, 0),
                )
            )
        return views


def _apply_until(
    memory: _MemoryPublish,
    plan: PublishPlan,
    *,
    include_file: bool,
    include_pointer: bool,
) -> None:
    """Apply a plan and stop before the pointer when the load is interrupted."""
    for sql, params in (*plan.creates, *plan.chunk_inserts):
        memory.apply(sql, params)
    if not include_file:
        return
    memory.apply(*plan.file_insert)
    if include_pointer:
        memory.apply(*plan.pointer_update)


def test_interrupted_stage_leaves_readers_on_the_previous_file() -> None:
    old_hash = "a" * 64
    new_hash = "b" * 64
    memory = _MemoryPublish()
    old = build_publish_plan("0123", b"previous-complete", old_hash, "file-old")
    _apply_until(memory, old, include_file=True, include_pointer=True)
    new = build_publish_plan("0123", b"partial-next", new_hash, "file-new")
    _apply_until(memory, new, include_file=False, include_pointer=False)

    chosen = choose_published_file(
        pointer_file_id=memory.pointer,
        files=memory.views(),
        kv_sha256=old_hash,
    )

    assert memory.pointer == "file-old"
    assert chosen is not None
    assert chosen.file_id == "file-old"
    assert chosen.sha256 == old_hash
    assert all(item.file_id != "file-new" or not item.complete for item in memory.views())


def test_pointer_flip_before_kv_bump_keeps_the_previous_file() -> None:
    old_hash = "c" * 64
    new_hash = "d" * 64
    memory = _MemoryPublish()
    old = build_publish_plan("0123", b"previous-complete", old_hash, "file-old")
    new = build_publish_plan("0123", b"next-complete", new_hash, "file-new")
    _apply_until(memory, old, include_file=True, include_pointer=True)
    _apply_until(memory, new, include_file=True, include_pointer=True)

    during_bump = choose_published_file(
        pointer_file_id=memory.pointer,
        files=memory.views(),
        kv_sha256=old_hash,
    )
    after_bump = choose_published_file(
        pointer_file_id=memory.pointer,
        files=memory.views(),
        kv_sha256=new_hash,
    )

    memory.apply(_DELETE_CHUNK_SQL, ("0123", "file-old"))
    memory.apply(_DELETE_FILE_SQL, ("0123", "file-old"))
    after_delete = choose_published_file(
        pointer_file_id=memory.pointer,
        files=memory.views(),
        kv_sha256=new_hash,
    )

    assert memory.pointer == "file-new"
    assert during_bump is not None and during_bump.file_id == "file-old"
    assert after_bump is not None and after_bump.file_id == "file-new"
    assert after_delete is not None and after_delete.file_id == "file-new"
    assert old.pointer_update[1] == ("0123", "file-old")
    assert isinstance(old.pointer_update[1][0], str)


def test_incomplete_chunk_count_is_not_served() -> None:
    new_hash = "e" * 64
    view = EarlyVoteFileView(
        file_id="file-new",
        sha256=new_hash,
        chunk_count=4,
        chunks_present=2,
    )

    chosen = choose_published_file(
        pointer_file_id="file-new",
        files=(view,),
        kv_sha256=new_hash,
    )

    assert chosen is None
    assert not view.complete


def test_publish_plan_swaps_one_pointer_and_deletes_by_file_id() -> None:
    plan = build_publish_plan("0123", b"abc", "f" * 64, "file-new")
    flat = [sql for sql, _params in _plan_statements(plan)]

    delete_chunk = flat.index(_DELETE_CHUNK_SQL)
    delete_file = flat.index(_DELETE_FILE_SQL)
    first_chunk = flat.index(_CHUNK_INSERT_SQL)
    pointer = flat.index(_POINTER_UPSERT_SQL)
    assert delete_chunk < delete_file < first_chunk < pointer
    assert flat[pointer + 1 :] == []
    assert "file_id = ?" in _DELETE_CHUNK_SQL
    assert plan.pointer_update[0] == _POINTER_UPSERT_SQL
    assert plan.pointer_update[1] == ("0123", "file-new")
    assert all(params[0] == "0123" for _sql, params in plan.chunk_inserts)
    assert all(isinstance(params[0], str) for _sql, params in plan.chunk_inserts)
    assert not any(
        sql == "DELETE FROM early_vote_chunk WHERE source_election_id = ?" for sql in flat
    )


def _schema_results(names: list[str]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "success": True,
            "result": [{"success": True, "results": [{"name": name} for name in names]}],
        },
    )


def _current_schema(sql: str) -> httpx.Response:
    if "early_vote_chunk" in sql:
        names = ["source_election_id", "file_id", "chunk_index", "payload_b64"]
    else:
        names = ["source_election_id", "file_id", "sha256", "byte_length", "chunk_count"]
    return _schema_results(names)


class _MemoryD1:
    """In-test D1 and KV. Cleanup reads these rows, not the writer's memory."""

    def __init__(self) -> None:
        self.chunks: dict[tuple[str, str, str], str] = {}
        self.files: dict[tuple[str, str], str] = {}
        self.pointer: dict[str, str] = {}
        self.kv: dict[str, str] = {}
        self.order: list[str] = []
        self.batches: list[list[str]] = []
        self.deleted: list[str] = []
        self.delete_attempts: list[list[str]] = []
        self.selections: list[list[str]] = []
        self.pointer_params: list[list[str]] = []
        self.fail_chunk_on: int | None = None
        self.chunk_inserts = 0
        self.fail_file_insert = False
        self.fail_deletes = False
        self.fail_puts = 0
        self._seen: list[str] = []

    def seed(self, election: str, file_id: str, digest: str, *, pointer: bool) -> None:
        self.chunks[(election, file_id, "0")] = "YQ=="
        self.files[(election, file_id)] = digest
        self._remember(file_id)
        if pointer:
            self.pointer[election] = file_id

    def _remember(self, file_id: str) -> None:
        if file_id not in self._seen:
            self._seen.append(file_id)

    def file_ids(self, election: str) -> list[str]:
        present: list[str] = []
        seen: set[str] = set()
        candidates = list(self._seen)
        for election_id, file_id, _index in self.chunks:
            if election_id == election and file_id not in candidates:
                candidates.append(file_id)
        for election_id, file_id in self.files:
            if election_id == election and file_id not in candidates:
                candidates.append(file_id)
        for file_id in candidates:
            in_chunk = any(key[0] == election and key[1] == file_id for key in self.chunks)
            in_file = (election, file_id) in self.files
            if (in_chunk or in_file) and file_id not in seen:
                seen.add(file_id)
                present.append(file_id)
        return present

    def query(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        batch = body["batch"]
        sqls = [item["sql"] for item in batch]
        self.batches.append(sqls)
        if _DELETE_CHUNK_SQL in sqls and self.fail_deletes:
            attempted = [
                str(item["params"][1]) for item in batch if item["sql"] == _DELETE_CHUNK_SQL
            ]
            self.delete_attempts.append(attempted)
            self.order.append("delete-failed")
            return httpx.Response(200, json={"success": True, "result": [{"success": False}]})
        results = []
        for item in batch:
            params = [str(value) for value in item["params"]]
            outcome = self._apply_sql(item["sql"], params)
            if outcome is None:
                return httpx.Response(200, json={"success": True, "result": [{"success": False}]})
            results.append(outcome)
        return httpx.Response(200, json={"success": True, "result": results})

    def _apply_sql(self, sql: str, params: list[str]) -> dict[str, object] | None:
        sql = sql.strip()
        if "pragma_table_info" in sql:
            self.order.append("schema")
            if "early_vote_chunk" in sql:
                names = ["source_election_id", "file_id", "chunk_index", "payload_b64"]
            else:
                names = ["source_election_id", "file_id", "sha256", "byte_length", "chunk_count"]
            return {"success": True, "results": [{"name": name} for name in names]}
        if sql.startswith("CREATE"):
            return {"success": True, "results": []}
        if sql == _FILE_IDS_SQL:
            found = self.file_ids(params[0])
            self.selections.append(found)
            self.order.append("file-ids")
            return {"success": True, "results": [{"file_id": file_id} for file_id in found]}
        if "MAX(row_id)" in sql:
            return {"success": True, "results": [{"max_row": None}]}
        if sql.startswith("DELETE FROM early_vote_roster_"):
            return {"success": True, "results": []}
        if sql == _DAY_FILE_IDS_SQL:
            self.order.append("day-file-ids")
            return {"success": True, "results": []}
        if sql == _POINTER_SELECT_SQL:
            self.order.append("select-pointer")
            file_id = self.pointer.get(params[0])
            rows = [{"file_id": file_id}] if file_id is not None and file_id != "" else []
            return {"success": True, "results": rows}
        if sql == _FILE_ID_FOR_HASH_SQL:
            election, digest = params
            found = [
                file_id
                for (election_id, file_id), sha in self.files.items()
                if election_id == election and sha == digest
            ]
            self.order.append("hash-select")
            return {"success": True, "results": [{"file_id": file_id} for file_id in found]}
        if sql == _DELETE_CHUNK_SQL:
            election, file_id = params
            self.deleted.append(file_id)
            self.order.append("delete")
            self.chunks = {
                key: value
                for key, value in self.chunks.items()
                if not (key[0] == election and key[1] == file_id)
            }
            return {"success": True, "results": []}
        if sql == _DELETE_FILE_SQL:
            self.files.pop((params[0], params[1]), None)
            return {"success": True, "results": []}
        if sql == _CHUNK_INSERT_SQL:
            self.chunk_inserts += 1
            if self.fail_chunk_on is not None and self.chunk_inserts == self.fail_chunk_on:
                self.order.append("chunk-failed")
                return None
            election, file_id, index, payload = params
            self._remember(file_id)
            self.chunks[(election, file_id, index)] = payload
            self.order.append("chunk")
            return {"success": True, "results": []}
        if sql == _FILE_INSERT_SQL:
            if self.fail_file_insert:
                self.order.append("file-failed")
                return None
            self._remember(params[1])
            self.files[(params[0], params[1])] = params[2]
            self.order.append("file")
            return {"success": True, "results": []}
        if (
            "early_vote_file_aggregate" in sql
            or "early_vote_county_aggregate" in sql
            or "early_vote_aggregate_meta" in sql
        ):
            self.order.append("precompute")
            return {"success": True, "results": []}
        if sql == _POINTER_UPSERT_SQL:
            self.pointer[params[0]] = params[1]
            self.pointer_params.append(params)
            self.order.append("pointer")
            return {"success": True, "results": []}
        raise AssertionError("unexpected sql")

    def kv_get(self, request: httpx.Request) -> httpx.Response:
        key = unquote(request.url.path.rsplit("/", 1)[-1])
        if key not in self.kv:
            return httpx.Response(404)
        return httpx.Response(200, text=self.kv[key])

    def kv_put(self, request: httpx.Request) -> httpx.Response:
        if self.fail_puts > 0:
            self.fail_puts -= 1
            return httpx.Response(500, json={"success": False, "errors": []})
        key = unquote(request.url.path.rsplit("/", 1)[-1])
        self.kv[key] = request.content.decode()
        self.order.append("kv")
        return httpx.Response(200, json={"success": True})


def _apply_memory(state: _MemoryD1, payload: bytes) -> None:
    list_url = f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT}/d1/database"
    with respx.mock:
        respx.get(list_url).mock(
            return_value=httpx.Response(
                200,
                json={
                    "success": True,
                    "result": [{"name": "ev-53813", "uuid": "db-1"}],
                    "result_info": {"total_count": 1},
                },
            )
        )
        respx.post(f"{list_url}/db-1/query").mock(side_effect=state.query)
        respx.get(url__regex=r".*/storage/kv/.*").mock(side_effect=state.kv_get)
        respx.put(url__regex=r".*/storage/kv/.*").mock(side_effect=state.kv_put)
        with httpx.Client(base_url=CLOUDFLARE_API_BASE) as client:
            store = _cf_store(client)
            assert "_replaced_file_id" not in vars(store)
            sync_early_vote("53813", _envelope(payload), store)


def _seed_published(state: _MemoryD1, file_id: str, digest: str) -> None:
    state.seed("53813", file_id, digest, pointer=True)
    state.kv[hash_key("53813")] = digest
    state.kv[version_key("53813")] = "2"


def test_cloudflare_pointer_swap_is_one_statement_and_delete_follows_kv() -> None:
    state = _MemoryD1()
    _seed_published(state, "file-old", sha256_decoded(b"previous-complete"))

    _apply_memory(state, b"file-one")
    published = state.pointer["53813"]

    assert published != "file-old"
    assert "file-old" in state.file_ids("53813")
    assert state.deleted == []
    assert "delete" not in state.order
    assert state.order.index("pointer") < state.order.index("kv")

    def _chunk_or_file_delete(sql: str) -> bool:
        return sql.startswith("DELETE") and "aggregate" not in sql

    assert not any(_chunk_or_file_delete(sql) for batch in state.batches for sql in batch)
    pointer_batches = [sqls for sqls in state.batches if _POINTER_UPSERT_SQL in sqls]
    assert pointer_batches == [[_POINTER_UPSERT_SQL]]
    assert state.pointer_params[0][0] == "53813"
    assert isinstance(state.pointer_params[0][0], str)
    assert isinstance(state.pointer_params[0][1], str)

    state.order.clear()
    state.batches.clear()
    _apply_memory(state, b"file-two")

    assert state.deleted == ["file-old"]
    assert state.order.index("delete") < state.order.index("pointer")
    assert state.order.index("pointer") < state.order.index("kv")
    assert published in state.file_ids("53813")
    assert "file-old" not in state.file_ids("53813")
    pointer_batches = [sqls for sqls in state.batches if _POINTER_UPSERT_SQL in sqls]
    assert pointer_batches == [[_POINTER_UPSERT_SQL]]


def test_failed_chunk_batch_does_not_flip_the_pointer_or_bump_kv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("texas_turnout_scraper.early_vote_sync.CHUNK_RAW_BYTES", 1)
    monkeypatch.setattr("texas_turnout_scraper.early_vote_sync._INSERT_BATCH", 1)
    list_url = f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT}/d1/database"
    chunk_batches = 0
    saw_pointer = False

    def query(request: httpx.Request) -> httpx.Response:
        nonlocal chunk_batches, saw_pointer
        body = json.loads(request.content)
        sqls = [item["sql"] for item in body["batch"]]
        if any("pragma_table_info" in sql for sql in sqls):
            return _current_schema(sqls[0])
        if _POINTER_UPSERT_SQL in sqls:
            saw_pointer = True
        if sqls and sqls[0] == _CHUNK_INSERT_SQL:
            chunk_batches += 1
            if chunk_batches == 2:
                return httpx.Response(
                    200,
                    json={"success": True, "result": [{"success": False}]},
                )
        if sqls == [_POINTER_SELECT_SQL]:
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "result": [{"success": True, "results": [{"file_id": "file-old"}]}],
                },
            )
        return _ok_query()

    with respx.mock:
        respx.get(list_url).mock(
            return_value=httpx.Response(
                200,
                json={
                    "success": True,
                    "result": [{"name": "ev-53813", "uuid": "db-1"}],
                    "result_info": {"total_count": 1},
                },
            )
        )
        respx.post(f"{list_url}/db-1/query").mock(side_effect=query)
        respx.get(url__regex=r".*/storage/kv/.*").mock(return_value=httpx.Response(404))
        puts = respx.put(url__regex=r".*/storage/kv/.*").mock(
            return_value=httpx.Response(200, json={"success": True})
        )
        with httpx.Client(base_url=CLOUDFLARE_API_BASE) as client:
            with pytest.raises(EarlyVoteSyncError, match="D1 query failed"):
                sync_early_vote("53813", _envelope(b"abc"), _cf_store(client))

    assert chunk_batches == 2
    assert saw_pointer is False
    assert puts.call_count == 0


def test_legacy_schema_refuses_in_place_replace() -> None:
    list_url = f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT}/d1/database"
    saw_delete = False

    def query(request: httpx.Request) -> httpx.Response:
        nonlocal saw_delete
        body = json.loads(request.content)
        sqls = [item["sql"] for item in body["batch"]]
        if any(sql.startswith("DELETE") for sql in sqls):
            saw_delete = True
        if any("pragma_table_info('early_vote_chunk')" in sql for sql in sqls):
            return _schema_results(["source_election_id", "chunk_index", "payload_b64"])
        return _ok_query()

    with respx.mock:
        respx.get(list_url).mock(
            return_value=httpx.Response(
                200,
                json={
                    "success": True,
                    "result": [{"name": "ev-53813", "uuid": "db-1"}],
                    "result_info": {"total_count": 1},
                },
            )
        )
        respx.post(f"{list_url}/db-1/query").mock(side_effect=query)
        respx.get(url__regex=r".*/storage/kv/.*").mock(return_value=httpx.Response(404))
        puts = respx.put(url__regex=r".*/storage/kv/.*").mock(return_value=httpx.Response(200))
        with httpx.Client(base_url=CLOUDFLARE_API_BASE) as client:
            with pytest.raises(EarlyVoteSyncError, match="no file_id column"):
                sync_early_vote("53813", _envelope(b"file"), _cf_store(client))

    assert saw_delete is False
    assert puts.call_count == 0


def test_stale_file_ids_keep_pointer_and_kv_hash() -> None:
    stale = stale_file_ids(
        ["file-old", "", "file-old", "orphan", None, "hash-file"],
        pointer_file_id="file-old",
        kv_hash_file_ids=("hash-file",),
    )

    assert stale == ("orphan",)


def test_stale_file_ids_keep_day_scoped_files() -> None:
    stale = stale_file_ids(
        ["pointer", "day-a", "day-b", "orphan"],
        pointer_file_id="pointer",
        kv_hash_file_ids=(),
        day_file_ids=("day-a", "day-b"),
    )
    assert stale == ("orphan",)


def test_cap_budgets_retained_base64_copies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("texas_turnout_scraper.early_vote_sync.D1_DATABASE_CAP_BYTES", 12)
    assert estimated_stored_bytes(3) == 12
    assert_within_cap(3, "53813")
    with pytest.raises(EarlyVoteCapError, match="retained copies"):
        assert_within_cap(4, "53813")


def test_failed_chunk_insert_is_deleted_from_d1_on_the_next_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("texas_turnout_scraper.early_vote_sync.CHUNK_RAW_BYTES", 1)
    monkeypatch.setattr("texas_turnout_scraper.early_vote_sync._INSERT_BATCH", 1)
    state = _MemoryD1()
    _seed_published(state, "file-old", sha256_decoded(b"previous-complete"))
    state.fail_chunk_on = 2

    with pytest.raises(EarlyVoteSyncError, match="D1 query failed"):
        _apply_memory(state, b"abc")

    partials = [file_id for file_id in state.file_ids("53813") if file_id != "file-old"]
    assert partials
    assert state.pointer["53813"] == "file-old"
    assert state.pointer_params == []
    assert state.kv[hash_key("53813")] == sha256_decoded(b"previous-complete")
    orphan = partials[0]
    assert ("53813", orphan) not in state.files

    state.fail_chunk_on = None
    _apply_memory(state, b"abc")

    assert orphan in state.deleted
    assert orphan not in state.file_ids("53813")
    assert "file-old" in state.file_ids("53813")
    assert state.pointer["53813"] != "file-old"


def test_failed_file_insert_is_deleted_from_d1_on_the_next_run() -> None:
    state = _MemoryD1()
    _seed_published(state, "file-old", sha256_decoded(b"previous-complete"))
    state.fail_file_insert = True

    with pytest.raises(EarlyVoteSyncError, match="D1 query failed"):
        _apply_memory(state, b"file")

    partials = [file_id for file_id in state.file_ids("53813") if file_id != "file-old"]
    assert len(partials) == 1
    orphan = partials[0]
    assert ("53813", orphan) not in state.files
    assert state.pointer["53813"] == "file-old"
    assert state.pointer_params == []

    state.fail_file_insert = False
    _apply_memory(state, b"file")

    assert orphan in state.deleted
    assert orphan not in state.file_ids("53813")
    assert "file-old" in state.file_ids("53813")


def test_bump_failure_keeps_pointer_and_kv_file_and_drops_later_orphan() -> None:
    state = _MemoryD1()
    old = sha256_decoded(b"previous-complete")
    _seed_published(state, "file-old", old)
    state.fail_puts = 1

    with pytest.raises(EarlyVoteSyncError, match="KV write failed"):
        _apply_memory(state, b"staged-new")

    flipped = state.pointer["53813"]
    assert flipped != "file-old"
    assert state.kv[hash_key("53813")] == old
    assert ("53813", flipped) in state.files
    assert state.deleted == []
    state.seed("53813", "file-orphan", "d" * 64, pointer=False)
    state.fail_puts = 0

    _apply_memory(state, b"staged-newer")

    assert state.deleted == ["file-orphan"]
    assert "file-old" in state.file_ids("53813")
    assert flipped in state.file_ids("53813")
    assert "file-orphan" not in state.file_ids("53813")


def test_failed_stale_delete_retries_from_d1_rows() -> None:
    state = _MemoryD1()
    _seed_published(state, "file-old", sha256_decoded(b"previous-complete"))
    state.seed("53813", "file-orphan", "e" * 64, pointer=False)
    state.fail_deletes = True

    with pytest.raises(EarlyVoteSyncError, match="D1 query failed"):
        _apply_memory(state, b"after-failure")

    assert state.pointer["53813"] == "file-old"
    assert state.pointer_params == []
    assert state.kv[version_key("53813")] == "2"
    assert state.delete_attempts == [["file-orphan"]]
    assert state.deleted == []
    assert state.kv[hash_key("53813")] == sha256_decoded(b"previous-complete")

    state.seed("53813", "file-orphan-2", "f" * 64, pointer=False)
    state.fail_deletes = False
    marked = len(state.selections)
    _apply_memory(state, b"after-failure")

    cleanup_ids = state.selections[marked]
    assert state.deleted == [file_id for file_id in cleanup_ids if file_id != "file-old"]
    assert state.deleted == ["file-orphan", "file-orphan-2"]
    assert "file-old" in state.file_ids("53813")
    assert "file-orphan" not in state.file_ids("53813")
    assert "file-orphan-2" not in state.file_ids("53813")
