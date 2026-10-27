"""Build in-memory VEP indexes from parsed row cores (counts exported; VUIDs stay internal)."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from reports.karl_rove.ndjson import VepRowCore


@dataclass
class VepMatchIndex:
    cohort_vuids: dict[int, set[str]] = field(default_factory=lambda: defaultdict(set))
    registration_year_vuids: dict[int | None, set[str]] = field(
        default_factory=lambda: defaultdict(set)
    )

    def ingest(self, row: VepRowCore) -> None:
        self.cohort_vuids[row.vep_cohort_year].add(row.vuid)
        self.registration_year_vuids[row.registration_year].add(row.vuid)

    def cohort_frozen(self) -> dict[int, frozenset[str]]:
        return {year: frozenset(vuids) for year, vuids in self.cohort_vuids.items()}

    def registration_frozen(self) -> dict[int | None, frozenset[str]]:
        return {year: frozenset(vuids) for year, vuids in self.registration_year_vuids.items()}
