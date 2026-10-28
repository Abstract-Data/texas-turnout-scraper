"""Unit tests for the early-vote Workflow ingest (ADR-014). Synthetic data only."""

from __future__ import annotations

from datetime import date

import pytest

from texas_turnout_scraper.early_vote_workflow.batching import (
    batch_row_groups,
    halve_batch_size,
)
from texas_turnout_scraper.early_vote_workflow.cron_schedule import should_dispatch
from texas_turnout_scraper.early_vote_workflow.dry_run import dry_run_day_from_bytes
from texas_turnout_scraper.early_vote_workflow.hash_gate import day_unchanged
from texas_turnout_scraper.early_vote_workflow.kv_finalize import workflow_should_bump_kv
from texas_turnout_scraper.early_vote_workflow.logging_policy import (
    ALLOWED_LOG_KEYS,
    sanitize_log_fields,
)
from texas_turnout_scraper.early_vote_workflow.markers import StepMarker, is_step_done
from texas_turnout_scraper.early_vote_workflow.mode import parse_ev_sync_mode, writes_enabled
from texas_turnout_scraper.early_vote_workflow.plan import (
    build_instance_id,
    make_run_plan,
    plan_run_days,
)
from texas_turnout_scraper.early_vote_workflow.runner import (
    hash_short_circuit,
    run_workflow_sync,
)
from texas_turnout_scraper.early_vote_workflow_tables import WORKFLOW_SCHEMA_CREATES

HEADER = b"COUNTY,VOTER_NAME,ID_VOTER,VOTING_METHOD,PRECINCT\n"


def _synthetic_row(index: int) -> bytes:
    vuid = f"{index:010d}"
    return f"COUNTY01,SYNTH-{index},{vuid},IN-PERSON,0001\n".encode()


def _synthetic_file(rows: int) -> bytes:
    return HEADER + b"".join(_synthetic_row(i) for i in range(rows))


class _FakeSource:
    def __init__(self, payloads: dict[str, bytes], stored: dict[str, str]) -> None:
        self._payloads = payloads
        self._stored = stored

    def fetch_day_bytes(self, ev_date: str) -> bytes:
        return self._payloads[ev_date]

    def stored_day_sha256(self, ev_date: str) -> str | None:
        return self._stored.get(ev_date)


def test_ev_sync_mode_defaults_to_off() -> None:
    assert parse_ev_sync_mode(None) == "off"
    assert parse_ev_sync_mode("APPLY") == "apply"
    assert parse_ev_sync_mode("bogus") == "off"
    assert not writes_enabled("dry_run")


def test_hash_short_circuit() -> None:
    payload = _synthetic_file(2)
    report = dry_run_day_from_bytes("2026-10-20", payload, stored_sha256=None)
    assert report.status == "changed"
    assert hash_short_circuit(report.sha256, report.sha256)
    assert day_unchanged(report.sha256, report.sha256)
    assert not day_unchanged("abc", report.sha256)


def test_dry_run_unchanged_counts_only() -> None:
    payload = _synthetic_file(3)
    first = dry_run_day_from_bytes("2026-10-21", payload, stored_sha256=None)
    second = dry_run_day_from_bytes("2026-10-21", payload, stored_sha256=first.sha256)
    assert second.status == "unchanged"
    assert second.est_rows == 3
    assert second.decoded_bytes > 0


def test_batching_splits_rows() -> None:
    rows = list(range(10_000))
    batches = batch_row_groups(rows, rows_per_statement=4000, statements_per_batch=4)
    assert len(batches) == 1
    assert sum(len(stmt) for batch in batches for stmt in batch) == 10_000
    assert halve_batch_size(4000) == 2000


def test_resume_markers() -> None:
    marker = StepMarker("run-1", "2026-10-20", "stage")
    markers: set[tuple[str, str, str, int | None]] = set()
    assert not is_step_done(markers, marker)
    markers.add(marker.key())
    assert is_step_done(markers, marker)


def test_workflow_runner_respects_mode_and_lease() -> None:
    cal = (date(2026, 10, 20), date(2026, 10, 21))
    plan = make_run_plan(
        election_id="53815",
        run_id="ev-53815-2026102010",
        run_mode="sweep",
        calendar_dates=cal,
        today=date(2026, 10, 21),
    )
    payloads = {
        "2026-10-20": _synthetic_file(1),
        "2026-10-21": _synthetic_file(2),
    }
    source = _FakeSource(payloads, {})
    report = run_workflow_sync(plan, "dry_run", source, lease_acquired=True)
    assert len(report.days) == 2
    assert all(day.status in ("changed", "unchanged", "skipped_empty") for day in report.days)
    locked = run_workflow_sync(plan, "dry_run", source, lease_acquired=False)
    assert locked.skipped_locked


def test_workflow_schema_in_loader_creates() -> None:
    from texas_turnout_scraper.early_vote_sync import _SCHEMA_CREATES

    for ddl in WORKFLOW_SCHEMA_CREATES:
        assert ddl in _SCHEMA_CREATES


def _kv_case(**kwargs: object) -> dict[str, object]:
    base = {
        "published_digest": "a",
        "files_written": 0,
        "finalize_after_sync": False,
        "pending_before": False,
        "pending_after": False,
        "kv_stored_hash": "a",
    }
    base.update(kwargs)
    return base


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        (_kv_case(published_digest=""), False),
        (_kv_case(files_written=1), True),
        (_kv_case(finalize_after_sync=True), True),
        (_kv_case(pending_before=True), True),
        (_kv_case(kv_stored_hash="b"), True),
        (_kv_case(), False),
    ],
)
def test_kv_bump_rule_branches(kwargs: dict[str, object], expected: bool) -> None:
    assert workflow_should_bump_kv(**kwargs) is expected


def test_logging_allowlist_strips_pii_keys() -> None:
    cleaned = sanitize_log_fields(
        {"rows": 1, "VOTER_NAME": "secret", "id_voter": "0123456789"},
    )
    assert cleaned == {"rows": 1}
    assert "VOTER_NAME" not in ALLOWED_LOG_KEYS


def test_plan_recent_vs_sweep() -> None:
    cal = (date(2026, 10, 19), date(2026, 10, 20), date(2026, 10, 21))
    sweep = plan_run_days(
        election_id="53815",
        calendar_dates=cal,
        run_mode="sweep",
        today=date(2026, 10, 21),
    )
    recent = plan_run_days(
        election_id="53815",
        calendar_dates=cal,
        run_mode="recent",
        today=date(2026, 10, 21),
    )
    assert len(sweep) == 3
    assert recent == ("2026-10-20", "2026-10-21")


def test_build_instance_id_format() -> None:
    from datetime import datetime, timezone

    when = datetime(2026, 10, 20, 15, 0, tzinfo=timezone.utc)
    assert build_instance_id("53815", when) == "ev-53815-2026102015"


def test_cron_gate_matches_existing_worker_tests() -> None:
    from datetime import datetime, timezone

    assert should_dispatch(datetime(2026, 10, 31, 15, 0, tzinfo=timezone.utc))
