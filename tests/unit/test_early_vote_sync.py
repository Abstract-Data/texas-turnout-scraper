"""Early-vote hash gate, unchanged exit, and the 10 GB D1 refusal."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import tempfile
from datetime import date
from pathlib import Path

import httpx
import pytest
import respx

from texas_turnout_scraper.civix.evr import API_PREFIX, BASE_URL
from texas_turnout_scraper.early_vote_sync import (
    _CHUNK_INSERT_SQL,
    _DELETE_CHUNK_SQL,
    _DELETE_FILE_SQL,
    _FILE_INSERT_SQL,
    _POINTER_SELECT_SQL,
    _POINTER_UPSERT_SQL,
    CHUNK_RAW_BYTES,
    CLOUDFLARE_API_BASE,
    D1_DATABASE_CAP_BYTES,
    KV_NOT_FOR_ELECTION_NIGHT,
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
    format_sync_report,
    hash_key,
    main,
    pull_early_vote_file,
    sha256_decoded,
    sync_early_vote,
    version_key,
)

FIXTURE = Path(__file__).parent.parent / "fixtures" / "early_vote" / "sample_envelope.json"
ACCOUNT = "00000000000000000000000000000001"
NAMESPACE = "ns-test"


class RecordingStore:
    def __init__(self, stored: str | None = None, *, fail_write: bool = False) -> None:
        self.stored = stored
        self.fail_write = fail_write
        self.calls: list[str] = []
        self.written: bytes | None = None
        self.version: str | None = None

    def stored_hash(self, election_id: str) -> str | None:
        self.calls.append(f"hash:{election_id}")
        return self.stored

    def write_database(self, election_id: str, decoded: bytes, digest: str) -> None:
        self.calls.append(f"write:{election_id}")
        if self.fail_write:
            raise EarlyVoteSyncError("D1 write failed")
        self.written = decoded

    def bump_version(self, election_id: str, digest: str) -> str:
        self.calls.append(f"bump:{election_id}")
        self.version = "2"
        return self.version

    def discard_replaced(self, election_id: str) -> None:
        self.calls.append(f"discard:{election_id}")


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
    assert store.calls == ["hash:0123"]


def test_unchanged_hash_exits_without_write() -> None:
    payload = b"same-file"
    digest = sha256_decoded(payload)
    store = RecordingStore(stored=digest)

    result = sync_early_vote("53813", _envelope(payload), store)

    assert result.status == "unchanged"
    assert result.exit_code == 0
    assert result.version is None
    assert store.calls == ["hash:53813"]
    assert store.written is None


def test_new_hash_writes_d1_before_kv_bump() -> None:
    payload = b"new-file"
    store = RecordingStore()

    result = sync_early_vote("53813", _envelope(payload), store)

    assert result.status == "wrote"
    assert result.version == "2"
    assert store.calls == ["hash:53813", "write:53813", "bump:53813", "discard:53813"]
    assert store.written == payload


def test_failed_d1_write_does_not_bump_kv() -> None:
    store = RecordingStore(fail_write=True)

    with pytest.raises(EarlyVoteSyncError, match="D1 write failed"):
        sync_early_vote("53813", _envelope(b"new-file"), store)

    assert store.calls == ["hash:53813", "write:53813"]
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
    assert store.calls == ["hash:53813"]
    assert store.written is None
    assert store.version is None


def test_over_cap_uses_the_real_limit_constant(monkeypatch: pytest.MonkeyPatch) -> None:
    assert D1_DATABASE_CAP_BYTES == 10_000_000_000
    assert_within_cap(D1_DATABASE_CAP_BYTES, "53813")
    with pytest.raises(EarlyVoteCapError, match="10000000001 bytes"):
        assert_within_cap(D1_DATABASE_CAP_BYTES + 1, "53813")

    monkeypatch.setattr(
        "texas_turnout_scraper.early_vote_sync.D1_DATABASE_CAP_BYTES",
        4,
    )
    store = RecordingStore()
    with pytest.raises(EarlyVoteCapError):
        sync_early_vote("53813", _envelope(b"12345"), store)
    assert store.calls == ["hash:53813"]


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
    assert store.calls == ["hash:53813"]


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
    assert store.calls == ["hash:53813"]


def test_invalid_election_id_is_rejected_before_a_write() -> None:
    store = RecordingStore()
    with pytest.raises(ValueError, match="election id"):
        sync_early_vote("0", _envelope(b"x"), store)
    assert store.calls == []


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
    assert "rows election_id=53813 source_rows=3 d1_chunk_rows=1 d1_file_rows=1 d1_rows=2" in (
        captured.out
    )
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

    assert flat.index(_POINTER_UPSERT_SQL) > max(
        index for index, sql in enumerate(flat) if sql == _CHUNK_INSERT_SQL
    )
    assert flat[-2:] == [_DELETE_CHUNK_SQL, _DELETE_FILE_SQL]
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


def test_cloudflare_pointer_swap_is_one_statement_and_delete_follows_kv() -> None:
    list_url = f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT}/d1/database"
    batches: list[list[str]] = []
    order: list[str] = []

    def query(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sqls = [item["sql"] for item in body["batch"]]
        batches.append(sqls)
        if any("pragma_table_info" in sql for sql in sqls):
            order.append("schema")
            return _current_schema(sqls[0])
        if sqls == [_POINTER_SELECT_SQL]:
            order.append("select-pointer")
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "result": [{"success": True, "results": [{"file_id": "file-old"}]}],
                },
            )
        if sqls == [_POINTER_UPSERT_SQL]:
            params = body["batch"][0]["params"]
            assert params == ["53813", body["batch"][0]["params"][1]]
            assert isinstance(params[0], str)
            assert isinstance(params[1], str)
            order.append("pointer")
            return _ok_query()
        if sqls[0] in {_DELETE_CHUNK_SQL, _DELETE_FILE_SQL}:
            assert body["batch"][0]["params"] == ["53813", "file-old"]
            assert body["batch"][1]["params"] == ["53813", "file-old"]
            order.append("delete")
            return _ok_query()
        order.append("write")
        return _ok_query()

    def put(request: httpx.Request) -> httpx.Response:
        order.append("kv")
        return httpx.Response(200, json={"success": True, "content": request.content.decode()})

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
        respx.put(url__regex=r".*/storage/kv/.*").mock(side_effect=put)
        with httpx.Client(base_url=CLOUDFLARE_API_BASE) as client:
            sync_early_vote("53813", _envelope(b"file"), _cf_store(client))

    pointer_batches = [sqls for sqls in batches if _POINTER_UPSERT_SQL in sqls]
    assert pointer_batches == [[_POINTER_UPSERT_SQL]]
    assert order.index("pointer") < order.index("kv")
    assert order.index("kv") < order.index("delete")
    assert "delete" not in order[: order.index("kv")]


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


def test_workflow_run_scripts_do_not_interpolate_inputs() -> None:
    workflow = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "early-vote-sync.yml"
    lines = workflow.read_text().splitlines()
    in_run = False
    run_blocks = 0
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("run:"):
            in_run = True
            run_blocks += 1
            assert "${{" not in line
            continue
        if in_run:
            if line.startswith("          ") or stripped == "":
                assert "${{" not in line
                continue
            in_run = False
    text = "\n".join(lines)
    assert run_blocks >= 4
    assert "group: early-vote-sync-${{ inputs.election_id }}" in text
    assert "cancel-in-progress: false" in text
    assert text.count("election_id must be digits only") == 2
    assert text.count("election_date must be MM/DD/YYYY") == 4
    assert "secrets." not in text.split("jobs:", 1)[1].split("apply:", 1)[0]
