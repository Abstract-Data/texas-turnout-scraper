from __future__ import annotations

from dataclasses import dataclass, field

from texas_turnout_scraper.early_vote_workflow.markers import StepMarker


@dataclass
class RunState:
    """In-memory resumability for unit tests and Workflow replays."""

    markers: set[tuple[str, str, str, int | None]] = field(default_factory=set)
    days_changed: int = 0
    days_unchanged: int = 0
    days_failed: int = 0
    files_written: int = 0

    def mark_done(self, marker: StepMarker) -> None:
        self.markers.add(marker.key())

    def done(self, marker: StepMarker) -> bool:
        return marker.key() in self.markers
