"""Workflow ordering: backfill finalizes KV only after precompute writes."""

from __future__ import annotations

from texas_turnout_scraper.early_vote_d1_precompute_backfill import run_precompute_backfill_on_store


class _FinalizeStore:
    def __init__(self) -> None:
        self.events: list[str] = []

    def _database_uuid_if_exists(self, election_id: str) -> str | None:  # noqa: SLF001
        return "db"

    def _ensure_schema(self, database_id: str) -> None:  # noqa: SLF001
        return None

    def _query_rows(
        self,
        database_id: str,
        sql: str,
        params: list[str],
    ) -> list[dict[str, object]]:  # noqa: SLF001
        return []

    def _write_precomputed_aggregates(  # noqa: SLF001
        self,
        database_id: str,
        election_id: str,
        file_id: str,
        decoded: bytes,
        *,
        ev_date_iso: str | None,
    ) -> None:
        self.events.append("write")

    def bump_version(self, election_id: str, digest: str) -> str:
        self.events.append(f"bump:{digest}")
        return "3"

    def close(self) -> None:
        return None


def test_backfill_finalize_kv_runs_after_precompute_pass() -> None:
    store = _FinalizeStore()
    result = run_precompute_backfill_on_store(store, "53815", dry_run=False)
    assert result.status == "unchanged"
    version = store.bump_version("53815", "deadbeef")
    assert version == "3"
    assert store.events == ["bump:deadbeef"]
