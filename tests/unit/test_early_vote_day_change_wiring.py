"""A changed day keeps its old version in R2 and logs added/removed voters before replacing it."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from texas_turnout_scraper.early_vote_sync import (
    CloudflareEarlyVoteStore,
    build_publish_plan,
)

HEADER = "ID_VOTER,VOTER_NAME,COUNTY,VOTING_METHOD\n"
OLD = (HEADER + "0000000001,A,TRAVIS,MAIL-IN\n0000000002,B,BEXAR,IN-PERSON\n").encode()
NEW = (HEADER + "0000000002,B,BEXAR,IN-PERSON\n0000000003,C,DALLAS,MAIL-IN\n").encode()


class _FakeSnapshots:
    def __init__(self, *, fail: bool = False) -> None:
        self.recorded: list[tuple[str, bytes]] = []
        self._fail = fail

    def record_version(
        self, election_id: str, ev_date: str, *, sha256: str, decoded: bytes, rows: int
    ) -> None:
        if self._fail:
            raise RuntimeError("r2 down")
        self.recorded.append((sha256, decoded))


def _store(
    snapshots: _FakeSnapshots | None,
    monkeypatch: pytest.MonkeyPatch,
    *,
    old: bool = True,
    pointer: str | None = None,
):
    store = CloudflareEarlyVoteStore(
        account_id="a",
        api_token="t",
        kv_namespace_id="n",
        client=httpx.Client(),
        snapshots=snapshots,
    )
    statements: list[tuple[str, list[str]]] = []

    def query_rows(self: Any, database_id: str, sql: str, params: list[str]) -> list[dict]:
        if "FROM early_vote_pointer" in sql:
            return [{"file_id": pointer}] if pointer else []
        if "FROM early_vote_day" in sql and old:
            return [{"file_id": "old-file", "sha256": "oldsha"}]
        if sql.startswith("SELECT (SELECT COUNT(*)"):
            return [{"row_count": 2, "reject_count": 0}]
        return []

    monkeypatch.setattr(CloudflareEarlyVoteStore, "_query_rows", query_rows)
    monkeypatch.setattr(
        CloudflareEarlyVoteStore, "_statements", lambda self, db, stmts: statements.extend(stmts)
    )
    monkeypatch.setattr(CloudflareEarlyVoteStore, "_insert_chunks", lambda self, db, ins: None)
    monkeypatch.setattr(
        CloudflareEarlyVoteStore, "_write_precomputed_aggregates", lambda self, *a, **k: None
    )
    monkeypatch.setattr(CloudflareEarlyVoteStore, "_load_file_bytes", lambda self, *a: OLD)
    return store, statements


def _write(store: CloudflareEarlyVoteStore) -> None:
    plan = build_publish_plan("53815", NEW, "newsha", "new-file")
    store._write_day_scoped("db", "53815", "2026-10-19", plan, "newsha", NEW)


def test_changed_day_snapshots_both_versions_and_logs_the_diff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshots = _FakeSnapshots()
    store, statements = _store(snapshots, monkeypatch)
    _write(store)
    assert [sha for sha, _ in snapshots.recorded] == ["oldsha", "newsha"]
    changes = [s for s in statements if "early_vote_day_change" in s[0]]
    assert changes
    params = [p for _, ps in changes for p in ps]
    assert "0000000003" in params
    assert "0000000001" in params
    assert "0000000002" not in params
    assert "A" not in params
    log_index = statements.index(changes[0])
    delete_index = next(
        i for i, s in enumerate(statements) if s[0].startswith("DELETE FROM early_vote_file ")
    )
    assert log_index < delete_index


def test_snapshot_failure_aborts_before_the_old_version_is_deleted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, statements = _store(_FakeSnapshots(fail=True), monkeypatch)
    with pytest.raises(RuntimeError):
        _write(store)
    assert not any(s[0].startswith("DELETE") for s in statements)


def test_first_load_of_a_day_snapshots_only_the_new_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshots = _FakeSnapshots()
    store, statements = _store(snapshots, monkeypatch, old=False)
    _write(store)
    assert [sha for sha, _ in snapshots.recorded] == ["newsha"]
    assert not any("early_vote_day_change" in s[0] for s in statements)


def test_without_r2_the_diff_is_still_logged(monkeypatch: pytest.MonkeyPatch) -> None:
    store, statements = _store(None, monkeypatch)
    _write(store)
    assert any("early_vote_day_change" in s[0] for s in statements)


def _pointer_upserts(statements: list[tuple[str, list[str]]]) -> list[list[str]]:
    return [p for sql, p in statements if sql.startswith("INSERT INTO early_vote_pointer")]


def test_pointer_follows_a_replaced_file_instead_of_dangling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, statements = _store(_FakeSnapshots(), monkeypatch, pointer="old-file")
    _write(store)
    assert _pointer_upserts(statements) == [["53815", "new-file"]]
    pointer_index = next(
        i for i, s in enumerate(statements) if s[0].startswith("INSERT INTO early_vote_pointer")
    )
    delete_index = next(
        i for i, s in enumerate(statements) if s[0].startswith("DELETE FROM early_vote_file ")
    )
    assert pointer_index < delete_index


def test_pointer_to_another_file_is_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    store, statements = _store(_FakeSnapshots(), monkeypatch, pointer="some-other-file")
    _write(store)
    assert _pointer_upserts(statements) == []
