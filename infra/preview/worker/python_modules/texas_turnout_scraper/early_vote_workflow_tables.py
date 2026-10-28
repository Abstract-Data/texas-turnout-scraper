"""DDL for the Cloudflare Workflow early-vote sync (ADR-014).

Imported by ``early_vote_sync`` for schema apply and by unit tests. No sqlmodel.
"""

from __future__ import annotations

EARLY_VOTE_SYNC_LEASE_SQL = """
CREATE TABLE IF NOT EXISTS early_vote_sync_lease (
    election_id TEXT PRIMARY KEY,
    holder TEXT NOT NULL,
    expires_at INTEGER NOT NULL
)
"""

EARLY_VOTE_SYNC_DAY_RUN_SQL = """
CREATE TABLE IF NOT EXISTS early_vote_sync_day_run (
    run_id TEXT NOT NULL,
    ev_date TEXT NOT NULL,
    file_id TEXT NOT NULL,
    prior_sha256 TEXT NOT NULL,
    target_version INTEGER NOT NULL,
    PRIMARY KEY (run_id, ev_date)
)
"""

EARLY_VOTE_SYNC_CANDIDATE_SQL = """
CREATE TABLE IF NOT EXISTS early_vote_sync_candidate (
    run_id TEXT NOT NULL,
    ev_date TEXT NOT NULL,
    range_index INTEGER NOT NULL,
    id_voter TEXT NOT NULL,
    PRIMARY KEY (run_id, ev_date, range_index, id_voter)
)
"""

EARLY_VOTE_SYNC_DAY_MARKER_SQL = """
CREATE TABLE IF NOT EXISTS early_vote_sync_day_marker (
    run_id TEXT NOT NULL,
    ev_date TEXT NOT NULL,
    step TEXT NOT NULL,
    range_index INTEGER,
    PRIMARY KEY (run_id, ev_date, step, range_index)
)
"""

EARLY_VOTE_SYNC_STATUS_SQL = """
CREATE TABLE IF NOT EXISTS early_vote_sync_status (
    run_id TEXT PRIMARY KEY,
    election_id TEXT NOT NULL,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at INTEGER NOT NULL,
    finished_at INTEGER,
    days_changed INTEGER NOT NULL DEFAULT 0,
    days_unchanged INTEGER NOT NULL DEFAULT 0,
    days_failed INTEGER NOT NULL DEFAULT 0
)
"""

EARLY_VOTE_ROSTER_ROW_DAY_VOTER_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_early_vote_roster_row_day_voter
ON early_vote_roster_row (day, id_voter)
"""

WORKFLOW_SCHEMA_CREATES: tuple[str, ...] = (
    EARLY_VOTE_SYNC_LEASE_SQL,
    EARLY_VOTE_SYNC_DAY_RUN_SQL,
    EARLY_VOTE_SYNC_CANDIDATE_SQL,
    EARLY_VOTE_SYNC_DAY_MARKER_SQL,
    EARLY_VOTE_SYNC_STATUS_SQL,
    EARLY_VOTE_ROSTER_ROW_DAY_VOTER_INDEX_SQL,
)
