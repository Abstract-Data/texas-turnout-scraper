"""Backfill all published Civix EV roster dates into one election D1 database.

KV ``ev:<election>:version`` bumps **once** after backfill when at least one day
was written. KV ``ev:<election>:sha256`` stays the **published file content hash**
(latest loaded day). Optional manifest digest lives at ``ev:<election>:manifest_sha256``.

Per-day idempotency uses D1 ``early_vote_day`` rows; re-running backfill does
not duplicate chunk rows for a day whose redacted payload hash is unchanged.

No voterfile joins (Tex. Elec. Code §18.066). Logs and reports are aggregates only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Literal

from texas_turnout_scraper.civix.envelope import _decode_envelope
from texas_turnout_scraper.civix.evr import CivixClient
from texas_turnout_scraper.civix.ids import require_election_id
from texas_turnout_scraper.early_vote_roster import summarize_roster_csv
from texas_turnout_scraper.early_vote_sync import (
    EarlyVotePayloadKind,
    EarlyVoteStore,
    EarlyVoteSyncError,
    backfill_manifest_digest,
    iso_ev_date,
    prepare_payload_bytes,
    pull_statewide_roster_file,
    sync_early_vote,
)

logger = logging.getLogger(__name__)

DEFAULT_NOV_2026_GENERAL_ID = "53815"
DEFAULT_NOV_2026_GENERAL_NAME = "2026 NOVEMBER GENERAL ELECTION"

SyncMode = Literal["single", "backfill", "latest"]


@dataclass(frozen=True)
class BackfillDayResult:
    """One EV date in a backfill run (counts only)."""

    ev_date_iso: str
    status: str
    sha256: str
    decoded_bytes: int


@dataclass(frozen=True)
class BackfillDatePlan:
    """Calendar vs load plan (ISO dates only — safe for logs)."""

    calendar_ev_days_total: int
    calendar_first_iso: str
    calendar_last_iso: str
    skipped_future_dates: tuple[str, ...]
    skipped_empty_dates: tuple[str, ...]
    load_dates: tuple[date, ...]

    @property
    def load_dates_iso(self) -> tuple[str, ...]:
        return tuple(iso_ev_date(day) for day in self.load_dates)


@dataclass(frozen=True)
class BackfillResult:
    """Outcome of a multi-day backfill."""

    election_id: str
    payload_kind: EarlyVotePayloadKind
    plan: BackfillDatePlan
    days: tuple[BackfillDayResult, ...]
    days_unchanged: int
    days_wrote: int
    days_dry_run: int
    days_skipped_empty: int
    manifest_sha256: str
    kv_version: str | None
    exit_code: int


def find_election_ev_dates(
    election_id: str,
    *,
    elections: list[Any] | None = None,
    client: CivixClient | None = None,
) -> tuple[str, date, tuple[date, ...]]:
    """Return election name, election day, and sorted published EV dates.

    When ``elections`` is omitted, ``client`` or a short-lived ``CivixClient``
    loads the live ``EVR_ELECTION`` index.
    """
    election_id = require_election_id(election_id)
    if elections is None:
        owned = client is None
        active = client or CivixClient()
        try:
            elections = active.list_elections()
        finally:
            if owned:
                active.close()
    match = None
    for item in elections:
        sid = getattr(item, "source_election_id", None) or str(getattr(item, "id", ""))
        if sid == election_id:
            match = item
            break
    if match is None:
        raise EarlyVoteSyncError(f"election_id {election_id} not found in Civix EVR index")
    name = str(getattr(match, "election_name", ""))
    election_day = getattr(match, "election_date", None)
    if not isinstance(election_day, date):
        raise EarlyVoteSyncError(f"election_id {election_id} missing election_date in index")
    ev_dates = []
    for entry in getattr(match, "early_voting_dates", ()) or ():
        day = getattr(entry, "date", None)
        if isinstance(day, date):
            ev_dates.append(day)
    if not ev_dates:
        raise EarlyVoteSyncError(f"election_id {election_id} has no early_voting_dates in index")
    return name, election_day, tuple(sorted(ev_dates))


def roster_day_has_rows(redacted: bytes) -> bool:
    """True when redacted statewide CSV has at least one data row."""
    if not redacted.strip():
        return False
    summary = summarize_roster_csv(redacted, statewide=True)
    return summary.est_rows > 0


def probe_statewide_roster_day(
    election_id: str,
    ev_day: date,
    *,
    http_backend: str = "httpx",
) -> tuple[bool, int, int]:
    """Return ``(has_rows, decoded_bytes, est_rows)`` without logging row bodies."""
    backend = "cloudscraper" if http_backend == "cloudscraper" else "httpx"
    response = pull_statewide_roster_file(election_id, ev_day, http_backend=backend)
    raw = _decode_envelope(response)
    redacted, summary = prepare_payload_bytes(raw, "statewide_roster")
    rows = summary.est_rows if summary is not None else 0
    has_rows = roster_day_has_rows(redacted)
    return has_rows, len(redacted), rows


def plan_backfill_load_dates(
    election_id: str,
    calendar_dates: tuple[date, ...],
    *,
    today: date | None = None,
    http_backend: str = "httpx",
) -> BackfillDatePlan:
    """Skip future calendar dates and days with empty roster payloads (no error)."""
    election_id = require_election_id(election_id)
    if not calendar_dates:
        raise EarlyVoteSyncError("no calendar EV dates to plan")
    cutoff = today or datetime.now(tz=timezone.utc).date()
    skipped_future: list[str] = []
    skipped_empty: list[str] = []
    load: list[date] = []
    for ev_day in sorted(calendar_dates):
        iso = iso_ev_date(ev_day)
        if ev_day > cutoff:
            skipped_future.append(iso)
            logger.info(
                "early-vote backfill skip future election_id=%s ev_date=%s",
                election_id,
                iso,
            )
            continue
        has_rows, decoded_bytes, est_rows = probe_statewide_roster_day(
            election_id,
            ev_day,
            http_backend=http_backend,
        )
        if not has_rows:
            skipped_empty.append(iso)
            logger.info(
                "early-vote backfill skip empty election_id=%s ev_date=%s "
                "decoded_bytes=%s est_rows=%s",
                election_id,
                iso,
                decoded_bytes,
                est_rows,
            )
            continue
        load.append(ev_day)
        logger.info(
            "early-vote backfill will load election_id=%s ev_date=%s decoded_bytes=%s est_rows=%s",
            election_id,
            iso,
            decoded_bytes,
            est_rows,
        )
    return BackfillDatePlan(
        calendar_ev_days_total=len(calendar_dates),
        calendar_first_iso=iso_ev_date(calendar_dates[0]),
        calendar_last_iso=iso_ev_date(calendar_dates[-1]),
        skipped_future_dates=tuple(skipped_future),
        skipped_empty_dates=tuple(skipped_empty),
        load_dates=tuple(load),
    )


def latest_published_ev_date(ev_dates: tuple[date, ...], *, today: date | None = None) -> date:
    """Latest EV date in the index that is not after ``today`` (UTC calendar)."""
    if not ev_dates:
        raise EarlyVoteSyncError("no EV dates to choose from")
    cutoff = today or datetime.now(tz=timezone.utc).date()
    eligible = [day for day in ev_dates if day <= cutoff]
    if not eligible:
        raise EarlyVoteSyncError("no EV dates on or before today in Civix index")
    return max(eligible)


def latest_loadable_ev_date(
    election_id: str,
    calendar_dates: tuple[date, ...],
    *,
    today: date | None = None,
    http_backend: str = "httpx",
) -> date:
    """Newest eligible calendar day with a non-empty roster (probe newest-first)."""
    election_id = require_election_id(election_id)
    if not calendar_dates:
        raise EarlyVoteSyncError("no calendar EV dates to choose from")
    cutoff = today or datetime.now(tz=timezone.utc).date()
    eligible = sorted((day for day in calendar_dates if day <= cutoff), reverse=True)
    if not eligible:
        raise EarlyVoteSyncError("no EV dates on or before today in Civix index")
    for ev_day in eligible:
        has_rows, decoded_bytes, est_rows = probe_statewide_roster_day(
            election_id,
            ev_day,
            http_backend=http_backend,
        )
        if has_rows:
            logger.info(
                "early-vote latest loadable election_id=%s ev_date=%s "
                "decoded_bytes=%s est_rows=%s",
                election_id,
                iso_ev_date(ev_day),
                decoded_bytes,
                est_rows,
            )
            return ev_day
    raise EarlyVoteSyncError(
        f"election_id {election_id}: no EV dates with roster data on or before today"
    )


def run_backfill(
    election_id: str,
    store: EarlyVoteStore,
    plan: BackfillDatePlan,
    *,
    dry_run: bool = False,
    payload_kind: EarlyVotePayloadKind = "statewide_roster",
    http_backend: str = "httpx",
) -> BackfillResult:
    """Ingest each date in ``plan.load_dates``; bump KV once at the end when not dry-run."""
    election_id = require_election_id(election_id)
    if payload_kind != "statewide_roster":
        raise EarlyVoteSyncError("backfill supports payload_kind=statewide_roster only")
    backend = "cloudscraper" if http_backend == "cloudscraper" else "httpx"
    day_results: list[BackfillDayResult] = []
    manifest_inputs: dict[str, str] = {}
    wrote = 0
    unchanged = 0
    dry_runs = 0

    for ev_day in plan.load_dates:
        iso = iso_ev_date(ev_day)
        response = pull_statewide_roster_file(
            election_id,
            ev_day,
            http_backend=backend,
        )
        raw = _decode_envelope(response)
        redacted, summary = prepare_payload_bytes(raw, "statewide_roster")
        if not roster_day_has_rows(redacted):
            logger.info(
                "early-vote backfill skip empty on load election_id=%s ev_date=%s",
                election_id,
                iso,
            )
            continue
        result = sync_early_vote(
            election_id,
            response,
            store,
            dry_run=dry_run,
            payload_kind=payload_kind,
            ev_date_iso=iso,
            defer_kv_bump=True,
        )
        manifest_inputs[iso] = result.sha256
        day_results.append(
            BackfillDayResult(
                ev_date_iso=iso,
                status=result.status,
                sha256=result.sha256,
                decoded_bytes=result.decoded_bytes,
            )
        )
        if result.status == "wrote":
            wrote += 1
        elif result.status == "unchanged":
            unchanged += 1
        elif result.status == "dry_run":
            dry_runs += 1
        if summary is not None:
            logger.info(
                "early-vote backfill day election_id=%s ev_date=%s status=%s "
                "decoded_bytes=%s est_rows=%s csv_columns=%s",
                election_id,
                iso,
                result.status,
                result.decoded_bytes,
                summary.est_rows,
                len(summary.columns),
            )

    manifest = backfill_manifest_digest(manifest_inputs) if manifest_inputs else ""
    kv_version: str | None = None
    if not dry_run and wrote > 0 and day_results:
        latest_day = max(day_results, key=lambda item: item.ev_date_iso)
        kv_version = store.finalize_published_roster(
            election_id,
            latest_day.ev_date_iso,
            latest_day.sha256,
            manifest_sha256=manifest or None,
        )
        logger.info(
            "early-vote backfill complete election_id=%s load_days=%s "
            "wrote=%s unchanged=%s published_ev_date=%s version=%s",
            election_id,
            len(plan.load_dates),
            wrote,
            unchanged,
            latest_day.ev_date_iso,
            kv_version,
        )
    elif dry_run:
        logger.info(
            "early-vote backfill dry-run election_id=%s load_days=%s manifest_sha256=%s",
            election_id,
            len(plan.load_dates),
            manifest,
        )

    return BackfillResult(
        election_id=election_id,
        payload_kind=payload_kind,
        plan=plan,
        days=tuple(day_results),
        days_unchanged=unchanged,
        days_wrote=wrote,
        days_dry_run=dry_runs,
        days_skipped_empty=len(plan.skipped_empty_dates),
        manifest_sha256=manifest,
        kv_version=kv_version,
        exit_code=0,
    )


def format_backfill_report(result: BackfillResult) -> str:
    """Operator report — day list shows ISO dates and statuses only."""
    plan = result.plan
    lines = [
        f"election_id={result.election_id}",
        f"payload_kind={result.payload_kind}",
        "sync_mode=backfill",
        f"calendar_ev_days_total={plan.calendar_ev_days_total}",
        f"calendar_range={plan.calendar_first_iso}..{plan.calendar_last_iso}",
        f"skipped_future_count={len(plan.skipped_future_dates)}",
        f"skipped_empty_count={len(plan.skipped_empty_dates)}",
        f"load_plan_count={len(plan.load_dates)}",
        f"load_plan_dates={','.join(plan.load_dates_iso)}",
        f"skipped_future_dates={','.join(plan.skipped_future_dates)}",
        f"skipped_empty_dates={','.join(plan.skipped_empty_dates)}",
        f"ev_days_total={len(result.days)}",
        f"days_wrote={result.days_wrote}",
        f"days_unchanged={result.days_unchanged}",
        f"days_dry_run={result.days_dry_run}",
        f"days_skipped_empty={result.days_skipped_empty}",
        f"manifest_sha256={result.manifest_sha256}",
        f"exit_code={result.exit_code}",
    ]
    if result.kv_version is not None:
        lines.append(f"kv_version={result.kv_version}")
        lines.append("kv_bump=once_at_end_content_hash")
        lines.append(f"kv_manifest_key=ev:{result.election_id}:manifest_sha256")
    else:
        lines.append("kv_bump=deferred_or_dry_run_or_unchanged")
    for day in result.days:
        lines.append(
            f"day ev_date={day.ev_date_iso} status={day.status} "
            f"decoded_bytes={day.decoded_bytes} sha256={day.sha256}"
        )
    return "\n".join(lines)
