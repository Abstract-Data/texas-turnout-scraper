"""Errors shared by the early-vote loader modules, kept apart so they import without cycles."""

from __future__ import annotations


class EarlyVoteSyncError(RuntimeError):
    """The sync could not finish. KV was not bumped after a failed D1 write."""
