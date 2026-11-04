"""Defer-KV ingest wiring: sync report fields and precompute finalize (synthetic only)."""

from __future__ import annotations

import json
from datetime import date

from texas_turnout_scraper.early_vote_backfill import (
    BackfillDatePlan,
    BackfillDayResult,
    BackfillResult,
    backfill_manifest_digest,
    format_backfill_report,
)
from texas_turnout_scraper.early_vote_d1_precompute_backfill import (
    main as precompute_main,
)
from texas_turnout_scraper.early_vote_d1_precompute_backfill import (
    should_finalize_kv_version,
)


class _IngestFinalizeStore:
    def __init__(self, digest: str) -> None:
        self._digest = digest
        self.kv_stored_hash: str | None = "stale-kv"
        self.manifest_sha256: str | None = "old-manifest"
        self.finalize_calls: list[tuple[str, str, str | None]] = []

    def _database_uuid_if_exists(self, election_id: str) -> str | None:
        return "db-1"

    def _ensure_schema(self, database_id: str) -> None:
        return None

    def _query_rows(self, database_id: str, sql: str, params: list[str]) -> list[dict[str, object]]:
        if "early_vote_file" in sql and "chunk_count" in sql:
            return [{"file_id": "f1", "sha256": self._digest, "chunk_count": 1}]
        if "early_vote_day" in sql and "ev_date = ?" in sql.replace("\n", " "):
            return [{"file_id": "f1"}]
        if "early_vote_pointer" in sql:
            return [{"file_id": "f1"}]
        if "early_vote_file_aggregate" in sql and "LIMIT 1" in sql:
            return [{"file_id": "f1"}]
        if "COUNT(*) AS n" in sql:
            return [{"n": 1}]
        if "chunk_index FROM early_vote_chunk" in sql.replace("\n", " "):
            return [{"chunk_index": 0}]
        if "payload_b64 FROM early_vote_chunk" in sql.replace("\n", " "):
            payload = json.dumps(
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
            import base64

            return [{"payload_b64": base64.b64encode(payload).decode()}]
        return []

    def _write_precomputed_aggregates(self, *args: object, **kwargs: object) -> None:
        return None

    def _record_aggregate_meta(self, *args: object, **kwargs: object) -> None:
        return None

    def stored_hash(self, election_id: str) -> str | None:
        return self.kv_stored_hash

    def finalize_published_roster(
        self,
        election_id: str,
        ev_date_iso: str,
        content_digest: str,
        *,
        manifest_sha256: str | None = None,
    ) -> str:
        self.finalize_calls.append((ev_date_iso, content_digest, manifest_sha256))
        self.kv_stored_hash = content_digest
        self.manifest_sha256 = manifest_sha256
        return "19"

    def close(self) -> None:
        return None


def test_sync_report_exposes_manifest_and_published_ev_date_for_ingest() -> None:
    digest = "a" * 64
    manifest = backfill_manifest_digest({"2026-11-02": digest, "2026-11-03": digest})
    plan = BackfillDatePlan(
        load_dates=(date(2026, 11, 2), date(2026, 11, 3)),
        calendar_ev_days_total=2,
        calendar_first_iso="2026-11-02",
        calendar_last_iso="2026-11-03",
        skipped_future_dates=(),
    )
    result = BackfillResult(
        election_id="53815",
        payload_kind="statewide_roster",
        plan=plan,
        days=(
            BackfillDayResult("2026-11-02", "unchanged", digest, 100),
            BackfillDayResult("2026-11-03", "wrote", digest, 200),
        ),
        days_unchanged=1,
        days_wrote=1,
        days_dry_run=0,
        skipped_empty_dates=(),
        failed_dates=(),
        manifest_sha256=manifest,
        kv_version=None,
        exit_code=0,
    )
    lines = format_backfill_report(result).splitlines()
    assert f"manifest_sha256={manifest}" in lines
    assert "published_ev_date=2026-11-03" in lines
    assert "status=wrote" in lines


def test_defer_finalize_after_sync_writes_manifest_not_bump_only(monkeypatch) -> None:
    digest = "b" * 64
    manifest = backfill_manifest_digest({"2026-11-03": digest})
    store = _IngestFinalizeStore(digest)
    monkeypatch.setattr(
        "texas_turnout_scraper.early_vote_d1_precompute_backfill._cloudflare_config_from_env",
        lambda: ("acct", "token", "ns"),
    )
    monkeypatch.setattr(
        "texas_turnout_scraper.early_vote_d1_precompute_backfill.CloudflareEarlyVoteStore",
        lambda **kwargs: store,
    )
    assert should_finalize_kv_version(
        published_digest=digest,
        files_written=0,
        finalize_after_sync=True,
        pending_before=False,
        pending_after=False,
        kv_stored_hash=store.kv_stored_hash,
    )
    code = precompute_main(
        [
            "--election-id",
            "53815",
            "--apply",
            "--finalize-kv-digest",
            digest,
            "--finalize-kv-after-sync",
            "--finalize-kv-manifest",
            manifest,
            "--finalize-kv-published-ev-date",
            "2026-11-03",
        ]
    )
    assert code == 0
    assert store.finalize_calls == [("2026-11-03", digest, manifest)]
    assert store.manifest_sha256 == manifest
    assert store.kv_stored_hash == digest
