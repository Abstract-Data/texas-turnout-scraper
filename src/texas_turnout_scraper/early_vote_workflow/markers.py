from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StepMarker:
    """Done-marker for a Workflow step (resumability)."""

    run_id: str
    ev_date: str
    step: str
    range_index: int | None = None

    def key(self) -> tuple[str, str, str, int | None]:
        return (self.run_id, self.ev_date, self.step, self.range_index)


def marker_step_name(base: str, range_index: int | None = None) -> str:
    if range_index is None:
        return base
    return f"{base}:{range_index:02d}"


def is_step_done(markers: set[tuple[str, str, str, int | None]], marker: StepMarker) -> bool:
    return marker.key() in markers
