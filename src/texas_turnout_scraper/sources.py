"""Roster source protocol — Civix and Legacy adapters share a common surface."""

from __future__ import annotations

from typing import Protocol

from .models import ElectionSummary, VoterRecord


class RosterSource(Protocol):
    """Protocol for fetching combined election rosters."""

    source_prefix: str

    def list_elections(self) -> list[ElectionSummary]: ...

    def fetch_election_rosters(
        self,
        election_id: str,
        *,
        pace_seconds: float = 1.0,
    ) -> tuple[list[VoterRecord], list[str], object | None]: ...
