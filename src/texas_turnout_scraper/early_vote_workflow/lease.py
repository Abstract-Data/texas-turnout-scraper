from __future__ import annotations

LEASE_TTL_SECONDS = 600

ACQUIRE_LEASE_SQL = """
INSERT INTO early_vote_sync_lease (election_id, holder, expires_at)
VALUES (?, ?, unixepoch() + ?)
ON CONFLICT(election_id) DO UPDATE SET
  holder = excluded.holder,
  expires_at = excluded.expires_at
WHERE early_vote_sync_lease.expires_at < unixepoch()
   OR early_vote_sync_lease.holder = excluded.holder
"""

RENEW_LEASE_SQL = """
UPDATE early_vote_sync_lease
SET expires_at = unixepoch() + ?
WHERE election_id = ? AND holder = ? AND expires_at > unixepoch()
"""

RELEASE_LEASE_SQL = """
DELETE FROM early_vote_sync_lease WHERE election_id = ? AND holder = ?
"""


def lease_holder_from_instance_id(instance_id: str) -> str:
    return instance_id


def acquire_succeeded(rows_changed: int) -> bool:
    return rows_changed == 1


def renew_succeeded(rows_changed: int) -> bool:
    return rows_changed == 1
