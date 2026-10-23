"""Idempotent precompute backfill planning against an in-memory D1 stub."""

from __future__ import annotations

import base64
import itertools
import json

import pytest

from texas_turnout_scraper.early_vote_d1_precompute_backfill import (
    _load_chunks_b64_paged,
    published_precompute_state,
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
        self.meta_records: list[tuple[str, int]] = []
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

    def _record_aggregate_meta(
        self,
        database_id: str,
        election_id: str,
        file_id: str,
        county_count: int,
    ) -> None:
        self.meta_records.append((file_id, county_count))

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
            return [{"file_id": f, "sha256": self._digest, "chunk_count": 1} for f in ("f1", "f2")]
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


_MANY_COUNTIES = json.dumps(
    {
        "turnout_by_county": [
            {
                "name": f"COUNTY{i:03d}",
                "total_in_person_votes_for_election": i,
                "total_mail_votes_for_election": 1,
            }
            for i in range(40)
        ]
    }
).encode()


class _CountingStore(_BackfillStore):
    """Real multi-chunk payloads plus a tally of D1 reads by kind."""

    def __init__(self, chunk_count: int, *, complete: bool, meta: bool = True) -> None:
        super().__init__("53815", "f1", _MANY_COUNTIES)
        size = len(_MANY_COUNTIES)
        bounds = [i * size // chunk_count for i in range(chunk_count + 1)]
        parts = [_MANY_COUNTIES[a:b] for a, b in itertools.pairwise(bounds)]
        assert all(parts)
        self._chunk_count = chunk_count
        self._chunks = {i: base64.b64encode(p).decode() for i, p in enumerate(parts)}
        self.reads: dict[str, int] = {}
        self.meta_count: int | None = None
        if complete:
            self._agg_rows.add("f1")
            self._county_rows["f1"] = 40
            if meta:
                self.meta_count = 40

    def _query_rows(self, database_id: str, sql: str, params: list[str]) -> list[dict[str, object]]:
        flat = sql.replace("\n", " ")
        if "early_vote_aggregate_meta" in flat:
            kind = "probe"
        elif "payload_b64" in flat:
            kind = "payload"
        elif "chunk_index FROM" in flat:
            kind = "chunk_index"
        else:
            kind = "other"
        self.reads[kind] = self.reads.get(kind, 0) + 1
        if kind == "probe":
            if self.meta_count is None or "f1" not in self._agg_rows:
                return []
            return [
                {
                    "expected": self.meta_count,
                    "actual": self._county_rows.get("f1", 0),
                    "chunk_count": self._chunk_count,
                    "chunks_present": len(self._chunks),
                }
            ]
        return super()._query_rows(database_id, sql, params)

    def _write_precomputed_aggregates(
        self,
        database_id: str,
        election_id: str,
        file_id: str,
        decoded: bytes,
        *,
        ev_date_iso: str | None,
    ) -> None:
        super()._write_precomputed_aggregates(
            database_id, election_id, file_id, decoded, ev_date_iso=ev_date_iso
        )
        self.meta_count = self._county_rows["f1"]

    def _record_aggregate_meta(
        self,
        database_id: str,
        election_id: str,
        file_id: str,
        county_count: int,
    ) -> None:
        super()._record_aggregate_meta(database_id, election_id, file_id, county_count)
        self.meta_count = county_count


def test_complete_file_is_confirmed_without_paging_payloads() -> None:
    store = _CountingStore(120, complete=True)
    result = run_precompute_backfill_on_store(store, "53815", dry_run=False)
    # before the probe: {'other': 3, 'chunk_index': 3, 'payload': 120} (126 reads)
    assert store.reads == {"other": 1, "probe": 1}
    assert result.files_skipped == 1
    assert result.files_written == 0
    assert store.writes == 0


def test_incomplete_file_still_reads_every_page_and_writes_same_rows() -> None:
    store = _CountingStore(120, complete=False)
    result = run_precompute_backfill_on_store(store, "53815", dry_run=False)
    assert store.reads["payload"] == 120
    assert store.reads["chunk_index"] == 3
    assert result.files_written == 1
    assert store._county_rows["f1"] == 40
    second = run_precompute_backfill_on_store(store, "53815", dry_run=False)
    assert second.files_skipped == 1
    assert store.writes == 1


def test_complete_rows_without_meta_take_full_path_once_then_get_meta() -> None:
    store = _CountingStore(120, complete=True, meta=False)
    first = run_precompute_backfill_on_store(store, "53815", dry_run=False)
    assert store.reads["payload"] == 120
    assert first.files_skipped == 1
    assert store.writes == 0
    assert store.meta_records == [("f1", 40)]
    store.reads.clear()
    second = run_precompute_backfill_on_store(store, "53815", dry_run=False)
    assert second.files_skipped == 1
    assert store.reads == {"other": 1, "probe": 1}


def test_dry_run_does_not_record_meta() -> None:
    store = _CountingStore(3, complete=True, meta=False)
    run_precompute_backfill_on_store(store, "53815", dry_run=True)
    assert store.meta_records == []


def test_dangling_pointer_is_not_an_unknown_target(monkeypatch) -> None:
    class _Dangling(_CountingStore):
        def _query_rows(
            self, database_id: str, sql: str, params: list[str]
        ) -> list[dict[str, object]]:
            if "early_vote_pointer" in sql:
                return [{"file_id": "ghost"}]
            return super()._query_rows(database_id, sql, params)

    store = _Dangling(3, complete=True)
    assert published_precompute_state(store, "53815", published_digest=store._digest) == "complete"
    store.kv_stored_hash = "stale-kv"
    bumped: list[str] = []
    assert _run_finalize(monkeypatch, store, store._digest, bumped) == 0
    assert bumped == [store._digest]


def test_meta_count_mismatch_falls_back_and_rewrites() -> None:
    store = _CountingStore(120, complete=True)
    store._county_rows["f1"] = 39
    result = run_precompute_backfill_on_store(store, "53815", dry_run=False)
    assert store.reads["payload"] == 120
    assert result.files_written == 1
    assert store._county_rows["f1"] == 40


@pytest.mark.parametrize("chunk_count", [1, 49, 50, 51])
def test_page_boundaries_unchanged_when_payloads_are_needed(chunk_count: int) -> None:
    store = _CountingStore(chunk_count, complete=False)
    result = run_precompute_backfill_on_store(store, "53815", dry_run=False)
    assert store.reads["payload"] == chunk_count
    assert store.reads["chunk_index"] == (chunk_count // 50) + 1
    assert result.files_written == 1


def test_probe_complete_state_does_not_page_payloads() -> None:
    store = _CountingStore(120, complete=True)
    state = published_precompute_state(store, "53815", published_digest=store._digest)
    assert state == "complete"
    assert "payload" not in store.reads


def test_probe_miss_with_unreadable_chunks_is_unknown_not_complete() -> None:
    store = _CountingStore(120, complete=True, meta=False)
    del store._chunks[7]
    state = published_precompute_state(store, "53815", published_digest=store._digest)
    assert state == "unknown"


def test_complete_aggregates_with_a_missing_chunk_are_unknown_not_complete() -> None:
    store = _CountingStore(120, complete=True)
    del store._chunks[7]
    state = published_precompute_state(store, "53815", published_digest=store._digest)
    assert state == "unknown"
    assert store.reads["payload"] >= 1  # fell through to the full path, then failed closed


def test_probe_sql_against_real_sqlite_rows() -> None:
    import sqlite3

    from texas_turnout_scraper.early_vote_d1_precompute_backfill import _COMPLETE_PROBE_SQL
    from texas_turnout_scraper.early_vote_d1_precompute_store import (
        aggregate_body_from_decoded,
        precompute_statements,
    )
    from texas_turnout_scraper.early_vote_sync import _CHUNK_TABLE_SQL, _FILE_TABLE_SQL

    conn = sqlite3.connect(":memory:")
    for ddl in (*PRECOMPUTE_SCHEMA_CREATES, _FILE_TABLE_SQL, _CHUNK_TABLE_SQL):
        conn.execute(ddl)
    body = aggregate_body_from_decoded(_MANY_COUNTIES, source_election_id="53815")
    for sql, params in precompute_statements("53815", "f1", body):
        conn.execute(sql, params)
    conn.execute("INSERT INTO early_vote_file VALUES ('53815', 'f1', 'sha', 10, 2)")
    for index in range(2):
        conn.execute("INSERT INTO early_vote_chunk VALUES ('53815', 'f1', ?, 'x')", (index,))

    def probe() -> list[tuple[int, int, int, int]]:
        rows = conn.execute(_COMPLETE_PROBE_SQL, ("53815", "f1")).fetchall()
        return [tuple(int(v) for v in row) for row in rows]  # type: ignore[misc]

    assert probe() == [(40, 40, 2, 2)]
    conn.execute("DELETE FROM early_vote_chunk WHERE chunk_index = 1")
    assert probe() == [(40, 40, 2, 1)]
    conn.execute("INSERT INTO early_vote_chunk VALUES ('53815', 'f1', 1, 'x')")
    conn.execute("DELETE FROM early_vote_county_aggregate WHERE county = 'COUNTY000'")
    assert probe() == [(40, 39, 2, 2)]
    conn.execute("DELETE FROM early_vote_aggregate_meta")
    assert probe() == []
