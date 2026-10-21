"""Idempotent precompute backfill planning against an in-memory D1 stub."""

from __future__ import annotations

import base64

from texas_turnout_scraper.early_vote_d1_precompute_backfill import run_precompute_backfill_on_store
from texas_turnout_scraper.early_vote_d1_precompute_store import PRECOMPUTE_SCHEMA_CREATES
from texas_turnout_scraper.early_vote_sync import sha256_decoded


class _BackfillStore:
    def __init__(self, election_id: str, file_id: str, decoded: bytes) -> None:
        self._election_id = election_id
        self._file_id = file_id
        self._digest = sha256_decoded(decoded)
        self._chunk = base64.b64encode(decoded).decode()
        self._schema: list[str] = []
        self._agg_rows: set[str] = set()
        self.writes = 0

    def _database_uuid_if_exists(self, election_id: str) -> str | None:
        return "db-1" if election_id == self._election_id else None

    def _ensure_schema(self, database_id: str) -> None:
        self._schema.extend(PRECOMPUTE_SCHEMA_CREATES)

    def _query_rows(self, database_id: str, sql: str, params: list[str]) -> list[dict[str, object]]:
        if "early_vote_file " in sql and "file_id" in sql:
            return [{"file_id": self._file_id, "sha256": self._digest}]
        if "early_vote_file_aggregate" in sql:
            if self._file_id in self._agg_rows:
                return [{"file_id": self._file_id}]
            return []
        if "early_vote_chunk" in sql:
            return [{"chunk_index": 0, "payload_b64": self._chunk}]
        if "early_vote_day" in sql:
            return [{"ev_date": "2026-10-20"}]
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


def test_backfill_idempotent_skip_when_rows_exist() -> None:
    decoded = b'{"turnout_by_county":[{"name":"TRAVIS","total_in_person_votes_for_election":1,"total_mail_votes_for_election":0}]}'
    store = _BackfillStore("53815", "f1", decoded)
    first = run_precompute_backfill_on_store(store, "53815", dry_run=False)
    assert first.files_written == 1
    assert store.writes == 1
    second = run_precompute_backfill_on_store(store, "53815", dry_run=False)
    assert second.files_written == 0
    assert second.files_skipped == 1
    assert store.writes == 1
