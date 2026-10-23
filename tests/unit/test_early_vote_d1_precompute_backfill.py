"""Idempotent precompute backfill planning against an in-memory D1 stub."""

from __future__ import annotations

import base64
import json

import pytest

from texas_turnout_scraper.early_vote_d1_precompute_backfill import (
    _load_chunks_b64_paged,
    run_precompute_backfill_on_store,
    should_finalize_kv_version,
)
from texas_turnout_scraper.early_vote_d1_precompute_store import PRECOMPUTE_SCHEMA_CREATES
from texas_turnout_scraper.early_vote_sync import EarlyVoteSyncError, sha256_decoded


class _BackfillStore:
    def __init__(
        self,
        election_id: str,
        file_id: str,
        decoded: bytes,
        *,
        chunk_count: int = 1,
    ) -> None:
        self._election_id = election_id
        self._file_id = file_id
        self._digest = sha256_decoded(decoded)
        self._chunk_count = chunk_count
        self._chunks = {
            index: base64.b64encode(decoded if index == 0 else b"").decode()
            for index in range(chunk_count)
        }
        if chunk_count == 1:
            self._chunks[0] = base64.b64encode(decoded).decode()
        self._schema: list[str] = []
        self._agg_rows: set[str] = set()
        self._county_rows: dict[str, int] = {}
        self.writes = 0
        self.batches: list[int] = []
        self.kv_stored_hash: str | None = None

    def _database_uuid_if_exists(self, election_id: str) -> str | None:
        return "db-1" if election_id == self._election_id else None

    def _ensure_schema(self, database_id: str) -> None:
        self._schema.extend(PRECOMPUTE_SCHEMA_CREATES)

    def _query_rows(self, database_id: str, sql: str, params: list[str]) -> list[dict[str, object]]:
        if "chunk_count" in sql and "early_vote_file" in sql:
            return [
                {
                    "file_id": self._file_id,
                    "sha256": self._digest,
                    "chunk_count": self._chunk_count,
                }
            ]
        if "chunk_index FROM early_vote_chunk" in sql.replace("\n", " "):
            limit = int(params[2])
            offset = int(params[3])
            indices = list(range(self._chunk_count))[offset : offset + limit]
            return [{"chunk_index": index} for index in indices]
        if "payload_b64 FROM early_vote_chunk" in sql.replace("\n", " "):
            index = int(params[2])
            payload = self._chunks.get(index)
            if payload is None:
                return []
            return [{"payload_b64": payload}]
        if "early_vote_file_aggregate" in sql and "LIMIT 1" in sql:
            if self._file_id in self._agg_rows:
                return [{"file_id": self._file_id}]
            return []
        if "COUNT(*) AS n" in sql:
            return [{"n": self._county_rows.get(self._file_id, 0)}]
        if "early_vote_day" in sql:
            return [{"ev_date": "2026-10-20"}]
        if "early_vote_pointer" in sql:
            return [{"file_id": self._file_id}]
        return []

    def _write_precomputed_aggregates(
        self,
        database_id: str,
        election_id: str,
        file_id: str,
        decoded: bytes,
        *,
        ev_date_iso: str | None,
    ) -> None:
        self.writes += 1
        self._agg_rows.add(file_id)
        payload = json.loads(decoded)
        counties = payload.get("turnout_by_county", [])
        self._county_rows[file_id] = len(counties) if isinstance(counties, list) else 0

    def bump_version(self, election_id: str, digest: str) -> str:
        self.kv_stored_hash = digest
        return "1"

    def stored_hash(self, election_id: str) -> str | None:
        return self.kv_stored_hash

    def close(self) -> None:
        return None


def test_backfill_refuses_short_chunk_set() -> None:
    decoded = (
        b'{"turnout_by_county":[{"name":"TRAVIS",'
        b'"total_in_person_votes_for_election":1,"total_mail_votes_for_election":0}]}'
    )
    store = _BackfillStore("53815", "f1", decoded, chunk_count=2)
    store._chunks = {0: base64.b64encode(decoded).decode()}
    result = run_precompute_backfill_on_store(store, "53815", dry_run=False)
    assert result.files_written == 0
    assert store.writes == 0


def test_backfill_pages_chunks_by_index() -> None:
    decoded = b'{"turnout_by_county":[]}'
    store = _BackfillStore("53815", "f1", decoded, chunk_count=3)
    store._chunks = {
        0: base64.b64encode(b"a").decode(),
        1: base64.b64encode(b"b").decode(),
        2: base64.b64encode(b"c").decode(),
    }
    loaded = _load_chunks_b64_paged(store, "db-1", "53815", "f1", expected_count=3)
    assert loaded is not None
    assert len(loaded) == 3


def test_backfill_idempotent_skip_when_rows_complete() -> None:
    decoded = json.dumps(
        {
            "turnout_by_county": [
                {
                    "name": "TRAVIS",
                    "total_in_person_votes_for_election": 1,
                    "total_mail_votes_for_election": 0,
                }
            ]
        }
    ).encode()
    store = _BackfillStore("53815", "f1", decoded)
    first = run_precompute_backfill_on_store(store, "53815", dry_run=False)
    assert first.files_written == 1
    assert store.writes == 1
    second = run_precompute_backfill_on_store(store, "53815", dry_run=False)
    assert second.files_written == 0
    assert second.files_skipped == 1
    assert store.writes == 1


def test_finalize_kv_digest_bumps_when_files_written(monkeypatch) -> None:
    store = _BackfillStore("53815", "f1", b'{"turnout_by_county":[]}')
    bumped: list[str] = []

    def _bump(election_id: str, digest: str) -> str:
        bumped.append(digest)
        return "9"

    store.bump_version = _bump  # type: ignore[method-assign]
    monkeypatch.setattr(
        "texas_turnout_scraper.early_vote_d1_precompute_backfill._cloudflare_config_from_env",
        lambda: ("acct", "token", "ns"),
    )
    monkeypatch.setattr(
        "texas_turnout_scraper.early_vote_d1_precompute_backfill.CloudflareEarlyVoteStore",
        lambda **kwargs: store,
    )
    from texas_turnout_scraper.early_vote_d1_precompute_backfill import main

    code = main(
        [
            "--election-id",
            "53815",
            "--apply",
            "--finalize-kv-digest",
            "abc123",
        ]
    )
    assert code == 0
    assert bumped == ["abc123"]


def test_finalize_kv_digest_skipped_when_unchanged_sync(monkeypatch) -> None:
    decoded = json.dumps(
        {
            "turnout_by_county": [
                {
                    "name": "TRAVIS",
                    "total_in_person_votes_for_election": 1,
                    "total_mail_votes_for_election": 0,
                }
            ]
        }
    ).encode()
    store = _BackfillStore("53815", "f1", decoded)
    store._agg_rows.add("f1")
    store._county_rows["f1"] = 1
    bumped: list[str] = []

    def _bump(election_id: str, digest: str) -> str:
        bumped.append(digest)
        return "9"

    store.bump_version = _bump  # type: ignore[method-assign]
    monkeypatch.setattr(
        "texas_turnout_scraper.early_vote_d1_precompute_backfill._cloudflare_config_from_env",
        lambda: ("acct", "token", "ns"),
    )
    monkeypatch.setattr(
        "texas_turnout_scraper.early_vote_d1_precompute_backfill.CloudflareEarlyVoteStore",
        lambda **kwargs: store,
    )
    from texas_turnout_scraper.early_vote_d1_precompute_backfill import main

    store.kv_stored_hash = "abc123"
    code = main(
        [
            "--election-id",
            "53815",
            "--apply",
            "--finalize-kv-digest",
            "abc123",
        ]
    )
    assert code == 0
    assert bumped == []


def test_retry_after_missing_precompute_bumps_without_after_sync(monkeypatch) -> None:
    """Simulates sync status=unchanged after defer; aggregates catch up on retry."""
    decoded = json.dumps(
        {
            "turnout_by_county": [
                {
                    "name": "TRAVIS",
                    "total_in_person_votes_for_election": 1,
                    "total_mail_votes_for_election": 0,
                }
            ]
        }
    ).encode()
    store = _BackfillStore("53815", "f1", decoded)
    store.kv_stored_hash = "stale-kv"
    bumped: list[str] = []

    def _bump(election_id: str, digest: str) -> str:
        bumped.append(digest)
        return "9"

    store.bump_version = _bump  # type: ignore[method-assign]
    monkeypatch.setattr(
        "texas_turnout_scraper.early_vote_d1_precompute_backfill._cloudflare_config_from_env",
        lambda: ("acct", "token", "ns"),
    )
    monkeypatch.setattr(
        "texas_turnout_scraper.early_vote_d1_precompute_backfill.CloudflareEarlyVoteStore",
        lambda **kwargs: store,
    )
    from texas_turnout_scraper.early_vote_d1_precompute_backfill import main

    digest = store._digest
    code = main(["--election-id", "53815", "--apply", "--finalize-kv-digest", digest])
    assert code == 0
    assert bumped == [digest]


def test_kv_stale_with_complete_aggregates_bumps_without_rewrite(monkeypatch) -> None:
    decoded = json.dumps(
        {
            "turnout_by_county": [
                {
                    "name": "TRAVIS",
                    "total_in_person_votes_for_election": 1,
                    "total_mail_votes_for_election": 0,
                }
            ]
        }
    ).encode()
    store = _BackfillStore("53815", "f1", decoded)
    store._agg_rows.add("f1")
    store._county_rows["f1"] = 1
    store.kv_stored_hash = "stale-kv"
    bumped: list[str] = []

    def _bump(election_id: str, digest: str) -> str:
        bumped.append(digest)
        return "9"

    store.bump_version = _bump  # type: ignore[method-assign]
    monkeypatch.setattr(
        "texas_turnout_scraper.early_vote_d1_precompute_backfill._cloudflare_config_from_env",
        lambda: ("acct", "token", "ns"),
    )
    monkeypatch.setattr(
        "texas_turnout_scraper.early_vote_d1_precompute_backfill.CloudflareEarlyVoteStore",
        lambda **kwargs: store,
    )
    from texas_turnout_scraper.early_vote_d1_precompute_backfill import main

    digest = store._digest
    code = main(["--election-id", "53815", "--apply", "--finalize-kv-digest", digest])
    assert code == 0
    assert bumped == [digest]


def test_should_finalize_kv_true_no_op_is_false() -> None:
    assert not should_finalize_kv_version(
        published_digest="abc",
        files_written=0,
        finalize_after_sync=False,
        pending_before=False,
        pending_after=False,
        kv_stored_hash="abc",
    )


def test_finalize_kv_after_sync_bumps_without_precompute_writes(monkeypatch) -> None:
    decoded = json.dumps(
        {
            "turnout_by_county": [
                {
                    "name": "TRAVIS",
                    "total_in_person_votes_for_election": 1,
                    "total_mail_votes_for_election": 0,
                }
            ]
        }
    ).encode()
    store = _BackfillStore("53815", "f1", decoded)
    store._agg_rows.add("f1")
    store._county_rows["f1"] = 1
    bumped: list[str] = []

    def _bump(election_id: str, digest: str) -> str:
        bumped.append(digest)
        return "9"

    store.bump_version = _bump  # type: ignore[method-assign]
    monkeypatch.setattr(
        "texas_turnout_scraper.early_vote_d1_precompute_backfill._cloudflare_config_from_env",
        lambda: ("acct", "token", "ns"),
    )
    monkeypatch.setattr(
        "texas_turnout_scraper.early_vote_d1_precompute_backfill.CloudflareEarlyVoteStore",
        lambda **kwargs: store,
    )
    from texas_turnout_scraper.early_vote_d1_precompute_backfill import main

    code = main(
        [
            "--election-id",
            "53815",
            "--apply",
            "--finalize-kv-digest",
            "sync-digest",
            "--finalize-kv-after-sync",
        ]
    )
    assert code == 0
    assert bumped == ["sync-digest"]


def test_backfill_rewrites_when_county_set_incomplete() -> None:
    decoded = json.dumps(
        {
            "turnout_by_county": [
                {
                    "name": "TRAVIS",
                    "total_in_person_votes_for_election": 1,
                    "total_mail_votes_for_election": 0,
                }
            ]
        }
    ).encode()
    store = _BackfillStore("53815", "f1", decoded)
    store._agg_rows.add("f1")
    store._county_rows["f1"] = 0
    result = run_precompute_backfill_on_store(store, "53815", dry_run=False)
    assert result.files_written == 1
    assert store.writes == 1


_ONE_COUNTY = json.dumps(
    {
        "turnout_by_county": [
            {
                "name": "TRAVIS",
                "total_in_person_votes_for_election": 1,
                "total_mail_votes_for_election": 0,
            }
        ]
    }
).encode()


class _TwoFileStore(_BackfillStore):
    """Pointer file ``f1`` is readable; digest-matched ``f2`` has no chunk payload."""

    def __init__(self) -> None:
        super().__init__("53815", "f1", _ONE_COUNTY)
        self.f2_readable = False

    def _query_rows(self, database_id: str, sql: str, params: list[str]) -> list[dict[str, object]]:
        flat = sql.replace("\n", " ")
        if "chunk_count" in sql and "early_vote_file" in sql:
            return [
                {"file_id": f, "sha256": self._digest, "chunk_count": 1} for f in ("f1", "f2")
            ]
        if "early_vote_pointer" in sql:
            return [{"file_id": "f1"}]
        if params and len(params) > 1 and params[1] == "f2":
            if "payload_b64 FROM early_vote_chunk" in flat and not self.f2_readable:
                return []
            if "chunk_index FROM early_vote_chunk" in flat:
                return [{"chunk_index": 0}]
            if "early_vote_file_aggregate" in sql and "LIMIT 1" in sql:
                return [{"file_id": "f2"}] if "f2" in self._agg_rows else []
            if "COUNT(*) AS n" in sql:
                return [{"n": self._county_rows.get("f2", 0)}]
            if "payload_b64 FROM early_vote_chunk" in flat:
                return [{"payload_b64": self._chunks[0]}]
        return super()._query_rows(database_id, sql, params)


def _run_finalize(monkeypatch, store: _BackfillStore, digest: str, bumped: list[str]) -> int:
    def _bump(election_id: str, value: str) -> str:
        bumped.append(value)
        store.kv_stored_hash = value
        return str(len(bumped))

    store.bump_version = _bump  # type: ignore[method-assign]
    monkeypatch.setattr(
        "texas_turnout_scraper.early_vote_d1_precompute_backfill._cloudflare_config_from_env",
        lambda: ("acct", "token", "ns"),
    )
    monkeypatch.setattr(
        "texas_turnout_scraper.early_vote_d1_precompute_backfill.CloudflareEarlyVoteStore",
        lambda **kwargs: store,
    )
    from texas_turnout_scraper.early_vote_d1_precompute_backfill import main

    return main(["--election-id", "53815", "--apply", "--finalize-kv-digest", digest])


def test_unreadable_target_with_stale_kv_hash_fails_without_bump(monkeypatch) -> None:
    store = _BackfillStore("53815", "f1", _ONE_COUNTY)
    del store._chunks[0]
    store.kv_stored_hash = "stale-kv"
    bumped: list[str] = []
    with pytest.raises(EarlyVoteSyncError, match="unreadable"):
        _run_finalize(monkeypatch, store, store._digest, bumped)
    assert bumped == []
    assert store.writes == 0


def test_precompute_state_distinguishes_complete_pending_unknown() -> None:
    from texas_turnout_scraper.early_vote_d1_precompute_backfill import published_precompute_state

    store = _BackfillStore("53815", "f1", _ONE_COUNTY)
    digest = store._digest
    assert published_precompute_state(store, "53815", published_digest=digest) == "pending"
    store._agg_rows.add("f1")
    store._county_rows["f1"] = 1
    assert published_precompute_state(store, "53815", published_digest=digest) == "complete"
    del store._chunks[0]
    store._agg_rows.clear()
    assert published_precompute_state(store, "53815", published_digest=digest) == "unknown"


def test_pending_target_still_bumps_after_backfill_writes(monkeypatch) -> None:
    store = _BackfillStore("53815", "f1", _ONE_COUNTY)
    store.kv_stored_hash = "stale-kv"
    bumped: list[str] = []
    assert _run_finalize(monkeypatch, store, store._digest, bumped) == 0
    assert bumped == [store._digest]
    assert store.writes == 1


def test_one_unreadable_target_among_many_blocks_bump_even_when_other_writes(monkeypatch) -> None:
    store = _TwoFileStore()
    store.kv_stored_hash = "stale-kv"
    bumped: list[str] = []
    with pytest.raises(EarlyVoteSyncError, match="unreadable"):
        _run_finalize(monkeypatch, store, store._digest, bumped)
    assert bumped == []


def test_retry_after_target_becomes_readable_bumps_exactly_once(monkeypatch) -> None:
    store = _TwoFileStore()
    store.kv_stored_hash = "stale-kv"
    bumped: list[str] = []
    with pytest.raises(EarlyVoteSyncError):
        _run_finalize(monkeypatch, store, store._digest, bumped)
    assert bumped == []
    store.f2_readable = True
    assert _run_finalize(monkeypatch, store, store._digest, bumped) == 0
    assert bumped == [store._digest]
    assert _run_finalize(monkeypatch, store, store._digest, bumped) == 0
    assert bumped == [store._digest]
