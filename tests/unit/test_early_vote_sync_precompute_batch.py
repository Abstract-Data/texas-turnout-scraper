"""Precomputed aggregate writes use one D1 batch per file replace."""

from __future__ import annotations

from unittest.mock import patch

from texas_turnout_scraper.early_vote_sync import CloudflareEarlyVoteStore


def test_write_precomputed_aggregates_uses_single_d1_batch() -> None:
    store = CloudflareEarlyVoteStore.__new__(CloudflareEarlyVoteStore)
    batch_sizes: list[int] = []

    def _record_batch(_database_id: str, statements: list[tuple[str, list[str]]]) -> None:
        batch_sizes.append(len(statements))

    decoded = (
        b'{"turnout_by_county":[{"name":"TRAVIS",'
        b'"total_in_person_votes_for_election":1,"total_mail_votes_for_election":0}]}'
    )
    with patch.object(store, "_statements", side_effect=_record_batch):
        store._write_precomputed_aggregates(
            "db",
            "53815",
            "f1",
            decoded,
            ev_date_iso="2026-10-20",
        )
    # 3 deletes + 1 county + meta + file aggregate row, still one batch
    assert batch_sizes == [6]


def _finalize_store(state: str):
    from unittest.mock import MagicMock

    store = CloudflareEarlyVoteStore.__new__(CloudflareEarlyVoteStore)
    store._database_uuid_if_exists = lambda election_id: "db"  # type: ignore[method-assign]
    store._query_rows = lambda *a, **k: [{"file_id": "f1"}]  # type: ignore[method-assign]
    store._statements = lambda *a, **k: None  # type: ignore[method-assign]
    store.backfill_precomputed_aggregates = lambda *a, **k: None  # type: ignore[method-assign]
    store.bump_version = MagicMock(return_value="7")  # type: ignore[method-assign]
    return store, state


def test_finalize_published_roster_refuses_to_bump_on_unknown_state() -> None:
    import pytest

    from texas_turnout_scraper.early_vote_sync import EarlyVoteSyncError

    store, _ = _finalize_store("unknown")
    with (
        patch(
            "texas_turnout_scraper.early_vote_d1_precompute_backfill.published_precompute_state",
            return_value="unknown",
        ),
        pytest.raises(EarlyVoteSyncError, match="unreadable"),
    ):
        store.finalize_published_roster("53815", "2026-10-20", "abc123")
    store.bump_version.assert_not_called()


def test_finalize_published_roster_bumps_once_when_state_is_known() -> None:
    store, _ = _finalize_store("complete")
    with patch(
        "texas_turnout_scraper.early_vote_d1_precompute_backfill.published_precompute_state",
        return_value="complete",
    ):
        assert store.finalize_published_roster("53815", "2026-10-20", "abc123") == "7"
    store.bump_version.assert_called_once_with("53815", "abc123")


def test_record_aggregate_meta_is_one_upsert_statement() -> None:
    store = CloudflareEarlyVoteStore.__new__(CloudflareEarlyVoteStore)
    sent: list[list[tuple[str, list[str]]]] = []
    with patch.object(store, "_statements", side_effect=lambda db, st: sent.append(st)):
        store._record_aggregate_meta("db", "53815", "f1", 40)
    assert len(sent) == 1 and len(sent[0]) == 1
    sql, params = sent[0][0]
    assert sql.startswith("INSERT OR REPLACE INTO early_vote_aggregate_meta")
    assert params == ["53815", "f1", "40"]
