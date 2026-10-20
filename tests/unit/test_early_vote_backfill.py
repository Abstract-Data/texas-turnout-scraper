"""Backfill enumeration, per-day idempotency, and single KV bump semantics."""

from __future__ import annotations

import base64
import json
from datetime import date
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from texas_turnout_scraper.early_vote_backfill import (
    DEFAULT_NOV_2026_GENERAL_ID,
    find_election_ev_dates,
    format_backfill_report,
    latest_published_ev_date,
    run_backfill,
)
from texas_turnout_scraper.early_vote_sync import (
    EarlyVoteSyncError,
    backfill_manifest_digest,
    sync_early_vote,
)


class _BackfillStore:
    def __init__(self) -> None:
        self.day_hashes: dict[tuple[str, str], str] = {}
        self.bump_count = 0

    def stored_hash(self, election_id: str, *, ev_date_iso: str | None = None) -> str | None:
        if ev_date_iso:
            return self.day_hashes.get((election_id, ev_date_iso))
        return None

    def write_database(
        self,
        election_id: str,
        decoded: bytes,
        digest: str,
        *,
        ev_date_iso: str | None = None,
    ) -> None:
        if not ev_date_iso:
            raise EarlyVoteSyncError("expected ev_date_iso")
        self.day_hashes[(election_id, ev_date_iso)] = digest

    def bump_version(self, election_id: str, digest: str) -> str:
        self.bump_count += 1
        return str(self.bump_count)

FIXTURE_INDEX = Path(__file__).parent.parent / "fixtures" / "early_voting" / "civix_election_index.json"
STATEWIDE_FIXTURE = (
    Path(__file__).parent.parent / "fixtures" / "early_vote" / "statewide_roster_envelope.json"
)


def _load_fixture_elections() -> list[object]:
    from texas_turnout_scraper.models import CivixElection

    raw = json.loads(FIXTURE_INDEX.read_text(encoding="utf-8"))["elections"]
    normalized = [{**e, "source_election_id": str(e["id"])} for e in raw]
    return [CivixElection.model_validate(item) for item in normalized]


def test_find_election_ev_dates_from_fixture() -> None:
    elections = _load_fixture_elections()
    name, election_day, ev_dates = find_election_ev_dates(
        DEFAULT_NOV_2026_GENERAL_ID,
        elections=elections,
    )
    assert "NOVEMBER GENERAL" in name
    assert election_day == date(2026, 11, 3)
    assert len(ev_dates) == 2
    assert ev_dates[0] < ev_dates[1]


def test_latest_published_ev_date_respects_today() -> None:
    ev_dates = (date(2026, 10, 14), date(2026, 10, 20))
    assert latest_published_ev_date(ev_dates, today=date(2026, 10, 18)) == date(2026, 10, 14)
    assert latest_published_ev_date(ev_dates, today=date(2026, 10, 25)) == date(2026, 10, 20)


def test_backfill_manifest_digest_is_order_independent() -> None:
    a = backfill_manifest_digest({"2026-10-14": "a" * 64, "2026-10-15": "b" * 64})
    b = backfill_manifest_digest({"2026-10-15": "b" * 64, "2026-10-14": "a" * 64})
    assert a == b


def _envelope_response() -> httpx.Response:
    body = STATEWIDE_FIXTURE.read_bytes()
    return httpx.Response(200, content=body)


def test_backfill_dry_run_lists_days_without_pii(capsys: pytest.CaptureFixture[str]) -> None:
    elections = _load_fixture_elections()
    _, _, ev_dates = find_election_ev_dates(DEFAULT_NOV_2026_GENERAL_ID, elections=elections)
    store = _BackfillStore()
    with patch(
        "texas_turnout_scraper.early_vote_backfill.pull_statewide_roster_file",
        return_value=_envelope_response(),
    ):
        result = run_backfill(
            DEFAULT_NOV_2026_GENERAL_ID,
            store,
            ev_dates,
            dry_run=True,
            payload_kind="statewide_roster",
        )
    report = format_backfill_report(result)
    assert result.days_dry_run == 2
    assert "ev_days_total=2" in report
    assert "kv_bump=deferred_or_dry_run" in report
    assert "SMITH" not in report
    assert store.bump_count == 0


def test_per_day_idempotency_and_single_kv_bump() -> None:
    payload = b"COUNTY,VOTER_NAME,ID_VOTER,VOTING_METHOD,PRECINCT\n"
    encoded = base64.b64encode(payload).decode("ascii")
    response = httpx.Response(200, json={"upload": encoded})
    store = _BackfillStore()
    ev_dates = (date(2026, 10, 14), date(2026, 10, 15))

    with patch(
        "texas_turnout_scraper.early_vote_backfill.pull_statewide_roster_file",
        return_value=response,
    ):
        first = run_backfill(
            DEFAULT_NOV_2026_GENERAL_ID,
            store,
            ev_dates,
            dry_run=False,
            payload_kind="statewide_roster",
        )
        second = run_backfill(
            DEFAULT_NOV_2026_GENERAL_ID,
            store,
            ev_dates,
            dry_run=False,
            payload_kind="statewide_roster",
        )

    assert first.days_wrote == 2
    assert second.days_unchanged == 2
    assert store.bump_count == 2
    assert first.kv_version is not None
    assert second.kv_version is not None


def test_sync_defer_kv_bump_skips_version() -> None:
    payload = b"x"
    store = _BackfillStore()
    sync_early_vote(
        "53815",
        httpx.Response(200, json={"upload": base64.b64encode(payload).decode("ascii")}),
        store,
        payload_kind="statewide_roster",
        ev_date_iso="2026-10-14",
        defer_kv_bump=True,
    )
    assert store.bump_count == 0
