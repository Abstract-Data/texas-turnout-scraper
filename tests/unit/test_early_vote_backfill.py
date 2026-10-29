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
    BackfillDatePlan,
    find_election_ev_dates,
    format_backfill_report,
    latest_loadable_ev_date,
    latest_published_ev_date,
    plan_backfill_load_dates,
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
        self.kv_content_hash: str | None = None
        self.manifest_sha256: str | None = None
        self.published_ev_date: str | None = None

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
        update_pointer: bool = False,
    ) -> None:
        if not ev_date_iso:
            raise EarlyVoteSyncError("expected ev_date_iso")
        self.day_hashes[(election_id, ev_date_iso)] = digest

    def bump_version(self, election_id: str, digest: str) -> str:
        self.bump_count += 1
        self.kv_content_hash = digest
        return str(self.bump_count)

    def finalize_published_roster(
        self,
        election_id: str,
        ev_date_iso: str,
        content_digest: str,
        *,
        manifest_sha256: str | None = None,
    ) -> str:
        self.bump_count += 1
        self.kv_content_hash = content_digest
        self.manifest_sha256 = manifest_sha256
        self.published_ev_date = ev_date_iso
        return str(self.bump_count)


_FIXTURE_ROOT = Path(__file__).parent.parent / "fixtures"
FIXTURE_INDEX = _FIXTURE_ROOT / "early_voting" / "civix_election_index.json"
STATEWIDE_FIXTURE = (
    Path(__file__).parent.parent / "fixtures" / "early_vote" / "statewide_roster_envelope.json"
)


def _load_fixture_elections() -> list[object]:
    from texas_turnout_scraper.models import CivixElection

    raw = json.loads(FIXTURE_INDEX.read_text(encoding="utf-8"))["elections"]
    normalized = [{**e, "source_election_id": str(e["id"])} for e in raw]
    return [CivixElection.model_validate(item) for item in normalized]


def _fixture_plan(ev_dates: tuple[date, ...]) -> BackfillDatePlan:
    return BackfillDatePlan(
        calendar_ev_days_total=len(ev_dates),
        calendar_first_iso=ev_dates[0].isoformat(),
        calendar_last_iso=ev_dates[-1].isoformat(),
        skipped_future_dates=(),
        load_dates=ev_dates,
    )


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
    with pytest.raises(EarlyVoteSyncError, match="on or before today"):
        latest_published_ev_date(ev_dates, today=date(2026, 10, 10))


def test_latest_loadable_probes_newest_first() -> None:
    calendar = (date(2026, 10, 14), date(2026, 10, 15), date(2026, 10, 16))
    calls: list[date] = []

    def fake_probe(_eid: str, ev_day: date, **_: object) -> tuple[bool, int, int]:
        calls.append(ev_day)
        return ev_day == date(2026, 10, 15), 10, 1

    with patch(
        "texas_turnout_scraper.early_vote_backfill.probe_statewide_roster_day",
        side_effect=fake_probe,
    ):
        picked = latest_loadable_ev_date(
            DEFAULT_NOV_2026_GENERAL_ID,
            calendar,
            today=date(2026, 10, 20),
        )
    assert picked == date(2026, 10, 15)
    assert calls == [date(2026, 10, 16), date(2026, 10, 15)]


def test_backfill_manifest_digest_is_order_independent() -> None:
    a = backfill_manifest_digest({"2026-10-14": "a" * 64, "2026-10-15": "b" * 64})
    b = backfill_manifest_digest({"2026-10-15": "b" * 64, "2026-10-14": "a" * 64})
    assert a == b


def _envelope_response() -> httpx.Response:
    body = STATEWIDE_FIXTURE.read_bytes()
    return httpx.Response(200, content=body)


def test_plan_backfill_skips_future_dates_without_pulling() -> None:
    calendar = (
        date(2026, 10, 14),
        date(2026, 10, 15),
        date(2026, 10, 21),
    )

    with patch(
        "texas_turnout_scraper.early_vote_backfill.pull_statewide_roster_file",
        side_effect=AssertionError("planning must not pull"),
    ):
        plan = plan_backfill_load_dates(
            DEFAULT_NOV_2026_GENERAL_ID,
            calendar,
            today=date(2026, 10, 20),
        )
    assert plan.calendar_ev_days_total == 3
    assert plan.load_dates == (date(2026, 10, 14), date(2026, 10, 15))
    assert plan.skipped_future_dates == ("2026-10-21",)


def test_backfill_dry_run_lists_days_without_pii(capsys: pytest.CaptureFixture[str]) -> None:
    elections = _load_fixture_elections()
    _, _, ev_dates = find_election_ev_dates(DEFAULT_NOV_2026_GENERAL_ID, elections=elections)
    plan = _fixture_plan(ev_dates)
    store = _BackfillStore()
    with patch(
        "texas_turnout_scraper.early_vote_backfill.pull_statewide_roster_file",
        return_value=_envelope_response(),
    ):
        result = run_backfill(
            DEFAULT_NOV_2026_GENERAL_ID,
            store,
            plan,
            dry_run=True,
            payload_kind="statewide_roster",
        )
    report = format_backfill_report(result)
    assert result.days_dry_run == 2
    assert "load_plan_dates=2026-10-14,2026-10-15" in report
    assert "ev_days_total=2" in report
    assert "kv_bump=deferred_or_dry_run" in report
    assert "SMITH" not in report
    assert store.bump_count == 0


def test_per_day_idempotency_and_single_kv_bump() -> None:
    payload = (
        b"COUNTY,VOTER_NAME,ID_VOTER,VOTING_METHOD,PRECINCT\nTEST,REDACTED,0000000001,IN-PERSON,1\n"
    )
    encoded = base64.b64encode(payload).decode("ascii")
    response = httpx.Response(200, json={"upload": encoded})
    store = _BackfillStore()
    ev_dates = (date(2026, 10, 14), date(2026, 10, 15))
    plan = _fixture_plan(ev_dates)

    with patch(
        "texas_turnout_scraper.early_vote_backfill.pull_statewide_roster_file",
        return_value=response,
    ):
        first = run_backfill(
            DEFAULT_NOV_2026_GENERAL_ID,
            store,
            plan,
            dry_run=False,
            payload_kind="statewide_roster",
        )
        second = run_backfill(
            DEFAULT_NOV_2026_GENERAL_ID,
            store,
            plan,
            dry_run=False,
            payload_kind="statewide_roster",
        )

    assert first.days_wrote == 2
    assert second.days_unchanged == 2
    assert store.bump_count == 1
    assert first.kv_version is not None
    assert second.kv_version is None
    assert store.published_ev_date == "2026-10-15"
    assert store.kv_content_hash == first.days[-1].sha256
    assert store.manifest_sha256 == first.manifest_sha256


def test_backfill_report_includes_sha256_and_status_for_workflow() -> None:
    elections = _load_fixture_elections()
    _, _, ev_dates = find_election_ev_dates(DEFAULT_NOV_2026_GENERAL_ID, elections=elections)
    plan = _fixture_plan(ev_dates)
    store = _BackfillStore()
    with patch(
        "texas_turnout_scraper.early_vote_backfill.pull_statewide_roster_file",
        return_value=_envelope_response(),
    ):
        result = run_backfill(
            DEFAULT_NOV_2026_GENERAL_ID,
            store,
            plan,
            dry_run=True,
            payload_kind="statewide_roster",
        )
    report = format_backfill_report(result)
    sha_line = next(line for line in report.splitlines() if line.startswith("sha256="))
    assert sha_line.removeprefix("sha256=")
    status_line = next(line for line in report.splitlines() if line.startswith("status="))
    assert status_line == "status=unchanged"


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


def _roster_response() -> httpx.Response:
    payload = (
        b"COUNTY,VOTER_NAME,ID_VOTER,VOTING_METHOD,PRECINCT\nTEST,REDACTED,0000000001,IN-PERSON,1\n"
    )
    return httpx.Response(200, json={"upload": base64.b64encode(payload).decode("ascii")})


def _portal_503(day: date) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://example.invalid/getFile")
    return httpx.HTTPStatusError(
        f"503 on {day.isoformat()}", request=request, response=httpx.Response(503, request=request)
    )


def _pull_failing_on(bad_day: date, exc: Exception | None = None):
    def fake_pull(_eid: str, ev_day: date, **_: object) -> httpx.Response:
        if ev_day == bad_day:
            raise exc or _portal_503(ev_day)
        return _roster_response()

    return fake_pull


def test_failed_newest_day_keeps_its_stored_hash_and_publishes_the_rest() -> None:
    store = _BackfillStore()
    ev_dates = (date(2026, 10, 14), date(2026, 10, 15))
    stored_newest = "c" * 64
    store.day_hashes[(DEFAULT_NOV_2026_GENERAL_ID, "2026-10-15")] = stored_newest

    with patch(
        "texas_turnout_scraper.early_vote_backfill.pull_statewide_roster_file",
        side_effect=_pull_failing_on(date(2026, 10, 15)),
    ):
        result = run_backfill(
            DEFAULT_NOV_2026_GENERAL_ID,
            store,
            _fixture_plan(ev_dates),
            payload_kind="statewide_roster",
        )

    report = format_backfill_report(result)
    assert result.days_wrote == 1
    assert result.failed_dates == ("2026-10-15",)
    assert result.exit_code == 0
    assert f"sha256={stored_newest}" in report.splitlines()
    assert "days_failed=1" in report.splitlines()
    assert "failed_dates=2026-10-15" in report.splitlines()
    # The newest day stays published as D1 last stored it.
    assert store.published_ev_date == "2026-10-15"
    assert store.kv_content_hash == stored_newest
    assert store.day_hashes[(DEFAULT_NOV_2026_GENERAL_ID, "2026-10-15")] == stored_newest


def test_failed_day_never_stored_is_left_out_of_the_publish() -> None:
    store = _BackfillStore()
    ev_dates = (date(2026, 10, 14), date(2026, 10, 15))

    with patch(
        "texas_turnout_scraper.early_vote_backfill.pull_statewide_roster_file",
        side_effect=_pull_failing_on(date(2026, 10, 15)),
    ):
        result = run_backfill(
            DEFAULT_NOV_2026_GENERAL_ID,
            store,
            _fixture_plan(ev_dates),
            payload_kind="statewide_roster",
        )

    written = store.day_hashes[(DEFAULT_NOV_2026_GENERAL_ID, "2026-10-14")]
    assert [day.ev_date_iso for day in result.days] == ["2026-10-14"]
    assert result.manifest_sha256 == backfill_manifest_digest({"2026-10-14": written})
    assert store.published_ev_date == "2026-10-14"
    assert f"sha256={written}" in format_backfill_report(result).splitlines()


def test_every_day_failing_with_nothing_stored_publishes_nothing() -> None:
    store = _BackfillStore()
    with patch(
        "texas_turnout_scraper.early_vote_backfill.pull_statewide_roster_file",
        side_effect=lambda _eid, ev_day, **_: (_ for _ in ()).throw(_portal_503(ev_day)),
    ):
        result = run_backfill(
            DEFAULT_NOV_2026_GENERAL_ID,
            store,
            _fixture_plan((date(2026, 10, 14),)),
            payload_kind="statewide_roster",
        )
    assert result.days == ()
    assert store.bump_count == 0
    assert "sha256=" in format_backfill_report(result).splitlines()


def test_cloudscraper_http_error_is_caught_too() -> None:
    requests = pytest.importorskip("requests")
    response = requests.Response()
    response.status_code = 503
    store = _BackfillStore()
    ev_dates = (date(2026, 10, 14), date(2026, 10, 15))

    with patch(
        "texas_turnout_scraper.early_vote_backfill.pull_statewide_roster_file",
        side_effect=_pull_failing_on(
            date(2026, 10, 14), requests.HTTPError("503", response=response)
        ),
    ):
        result = run_backfill(
            DEFAULT_NOV_2026_GENERAL_ID,
            store,
            _fixture_plan(ev_dates),
            payload_kind="statewide_roster",
        )
    assert result.failed_dates == ("2026-10-14",)
    assert result.days_wrote == 1


def test_store_error_during_sync_still_aborts() -> None:
    class _BrokenStore(_BackfillStore):
        def write_database(self, *args: object, **kwargs: object) -> None:
            raise EarlyVoteSyncError("D1 write failed")

    with (
        patch(
            "texas_turnout_scraper.early_vote_backfill.pull_statewide_roster_file",
            return_value=_roster_response(),
        ),
        pytest.raises(EarlyVoteSyncError, match="D1 write failed"),
    ):
        run_backfill(
            DEFAULT_NOV_2026_GENERAL_ID,
            _BrokenStore(),
            _fixture_plan((date(2026, 10, 14),)),
            payload_kind="statewide_roster",
        )
