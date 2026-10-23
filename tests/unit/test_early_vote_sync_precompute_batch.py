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
    assert batch_sizes == [4]
