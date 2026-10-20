"""D1 schema and pure roster anomaly detection for Civix early-vote VUIDs.

DDL constants mirror :mod:`early_vote_sync` (``CREATE TABLE IF NOT EXISTS`` plus
indexes). Nothing here writes to D1 or runs during sync; a future job will apply
these statements and persist rows produced by :func:`compare_early_vote_rosters`.

This module never logs ``id_voter`` values.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal

AnomalyKind = Literal[
    "within_file_dup",
    "dropped_from_later",
    "method_changed",
    "county_changed",
]

ANOMALY_KINDS: Final[tuple[AnomalyKind, ...]] = (
    "within_file_dup",
    "dropped_from_later",
    "method_changed",
    "county_changed",
)

_VUID_DUP_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS early_vote_vuid_dup (
    source_election_id TEXT NOT NULL,
    ev_date TEXT NOT NULL,
    file_id TEXT NOT NULL,
    prior_file_id TEXT,
    id_voter TEXT NOT NULL,
    anomaly_kind TEXT NOT NULL,
    county TEXT,
    voting_method TEXT,
    prior_county TEXT,
    prior_voting_method TEXT,
    occurrence_count INTEGER NOT NULL DEFAULT 1,
    CHECK (anomaly_kind IN (
        'within_file_dup',
        'dropped_from_later',
        'method_changed',
        'county_changed'
    )),
    CHECK (occurrence_count >= 1),
    PRIMARY KEY (source_election_id, ev_date, file_id, id_voter, anomaly_kind)
)
""".strip()

_VUID_DUP_INDEX_ELECTION_VOTER_SQL = """
CREATE INDEX IF NOT EXISTS idx_early_vote_vuid_dup_election_voter
ON early_vote_vuid_dup (source_election_id, id_voter)
""".strip()

_VUID_DUP_INDEX_ELECTION_DATE_KIND_SQL = """
CREATE INDEX IF NOT EXISTS idx_early_vote_vuid_dup_election_date_kind
ON early_vote_vuid_dup (source_election_id, ev_date, anomaly_kind)
""".strip()

_VUID_DUP_INDEX_ELECTION_FILE_SQL = """
CREATE INDEX IF NOT EXISTS idx_early_vote_vuid_dup_election_file
ON early_vote_vuid_dup (source_election_id, file_id)
""".strip()

_VUID_DUP_DAY_VIEW_SQL = """
CREATE VIEW IF NOT EXISTS early_vote_vuid_dup_day AS
SELECT
    source_election_id,
    ev_date,
    file_id,
    COUNT(DISTINCT CASE WHEN anomaly_kind = 'within_file_dup' THEN id_voter END)
        AS dup_vuids,
    COALESCE(
        SUM(
            CASE
                WHEN anomaly_kind = 'within_file_dup' THEN occurrence_count - 1
                ELSE 0
            END
        ),
        0
    ) AS dup_excess_rows,
    COUNT(
        DISTINCT CASE
            WHEN anomaly_kind = 'within_file_dup' AND prior_voting_method IS NOT NULL
            THEN id_voter
        END
    ) AS dup_multi_method,
    COUNT(
        DISTINCT CASE
            WHEN anomaly_kind = 'within_file_dup' AND prior_county IS NOT NULL
            THEN id_voter
        END
    ) AS dup_multi_county,
    COUNT(DISTINCT CASE WHEN anomaly_kind = 'dropped_from_later' THEN id_voter END)
        AS dropped_vuids,
    COUNT(DISTINCT CASE WHEN anomaly_kind = 'method_changed' THEN id_voter END)
        AS method_changed_vuids,
    COUNT(DISTINCT CASE WHEN anomaly_kind = 'county_changed' THEN id_voter END)
        AS county_changed_vuids
FROM early_vote_vuid_dup
GROUP BY source_election_id, ev_date, file_id
""".strip()

VUID_DUP_SCHEMA_CREATES: Final[tuple[str, ...]] = (
    _VUID_DUP_TABLE_SQL,
    _VUID_DUP_INDEX_ELECTION_VOTER_SQL,
    _VUID_DUP_INDEX_ELECTION_DATE_KIND_SQL,
    _VUID_DUP_INDEX_ELECTION_FILE_SQL,
    _VUID_DUP_DAY_VIEW_SQL,
)


@dataclass(frozen=True)
class EarlyVoteRosterRow:
    """One decoded roster row — no voter name."""

    county: str
    id_voter: str
    voting_method: str


@dataclass(frozen=True)
class EarlyVoteRosterSnapshot:
    """In-memory roster for one published file and EV calendar day."""

    source_election_id: str
    ev_date: str
    file_id: str
    rows: tuple[EarlyVoteRosterRow, ...]


@dataclass(frozen=True)
class EarlyVoteVuidDupRow:
    """One row matching the ``early_vote_vuid_dup`` D1 table."""

    source_election_id: str
    ev_date: str
    file_id: str
    prior_file_id: str | None
    id_voter: str
    anomaly_kind: AnomalyKind
    county: str | None
    voting_method: str | None
    prior_county: str | None
    prior_voting_method: str | None
    occurrence_count: int = 1


def _group_rows(rows: Sequence[EarlyVoteRosterRow]) -> dict[str, list[EarlyVoteRosterRow]]:
    grouped: dict[str, list[EarlyVoteRosterRow]] = defaultdict(list)
    for row in rows:
        grouped[row.id_voter].append(row)
    return grouped


def _second_distinct(values: Iterable[str], first: str) -> str | None:
    for value in values:
        if value != first:
            return value
    return None


def _within_file_dup_rows(snapshot: EarlyVoteRosterSnapshot) -> list[EarlyVoteVuidDupRow]:
    out: list[EarlyVoteVuidDupRow] = []
    for vuid, occurrences in _group_rows(snapshot.rows).items():
        if len(occurrences) < 2:
            continue
        counties = {row.county for row in occurrences}
        methods = {row.voting_method for row in occurrences}
        first = occurrences[0]
        out.append(
            EarlyVoteVuidDupRow(
                source_election_id=snapshot.source_election_id,
                ev_date=snapshot.ev_date,
                file_id=snapshot.file_id,
                prior_file_id=None,
                id_voter=vuid,
                anomaly_kind="within_file_dup",
                county=first.county,
                voting_method=first.voting_method,
                prior_county=_second_distinct(counties, first.county)
                if len(counties) > 1
                else None,
                prior_voting_method=_second_distinct(methods, first.voting_method)
                if len(methods) > 1
                else None,
                occurrence_count=len(occurrences),
            )
        )
    return out


def _canonical_maps(
    rows: Sequence[EarlyVoteRosterRow],
) -> tuple[Mapping[str, EarlyVoteRosterRow], Mapping[str, set[str]], Mapping[str, set[str]]]:
    grouped = _group_rows(rows)
    canonical: dict[str, EarlyVoteRosterRow] = {}
    counties: dict[str, set[str]] = {}
    methods: dict[str, set[str]] = {}
    for vuid, occ in grouped.items():
        canonical[vuid] = occ[0]
        counties[vuid] = {row.county for row in occ}
        methods[vuid] = {row.voting_method for row in occ}
    return canonical, counties, methods


def compare_early_vote_rosters(
    prior: EarlyVoteRosterSnapshot | None,
    current: EarlyVoteRosterSnapshot,
) -> tuple[EarlyVoteVuidDupRow, ...]:
    """Detect VUID anomalies between two decoded roster snapshots for one ``ev_date``.

    ``prior`` is the earlier published file for the same election day; ``current`` is
    the later file. When ``prior`` is ``None``, only ``within_file_dup`` rows on
    ``current`` are returned.
    """
    if prior is not None:
        if (
            prior.source_election_id != current.source_election_id
            or prior.ev_date != current.ev_date
        ):
            msg = "prior and current snapshots must share source_election_id and ev_date"
            raise ValueError(msg)

    results: list[EarlyVoteVuidDupRow] = _within_file_dup_rows(current)

    if prior is None:
        return tuple(results)

    prior_canonical, prior_counties, prior_methods = _canonical_maps(prior.rows)
    current_canonical, current_counties, current_methods = _canonical_maps(current.rows)

    prior_vuids = set(prior_canonical)
    current_vuids = set(current_canonical)

    for vuid in sorted(prior_vuids - current_vuids):
        prior_row = prior_canonical[vuid]
        results.append(
            EarlyVoteVuidDupRow(
                source_election_id=current.source_election_id,
                ev_date=current.ev_date,
                file_id=current.file_id,
                prior_file_id=prior.file_id,
                id_voter=vuid,
                anomaly_kind="dropped_from_later",
                county=None,
                voting_method=None,
                prior_county=prior_row.county,
                prior_voting_method=prior_row.voting_method,
                occurrence_count=1,
            )
        )

    for vuid in sorted(prior_vuids & current_vuids):
        cur_row = current_canonical[vuid]
        prev_row = prior_canonical[vuid]
        if prior_methods[vuid] != current_methods[vuid]:
            results.append(
                EarlyVoteVuidDupRow(
                    source_election_id=current.source_election_id,
                    ev_date=current.ev_date,
                    file_id=current.file_id,
                    prior_file_id=prior.file_id,
                    id_voter=vuid,
                    anomaly_kind="method_changed",
                    county=cur_row.county,
                    voting_method=cur_row.voting_method,
                    prior_county=prev_row.county,
                    prior_voting_method=prev_row.voting_method,
                    occurrence_count=1,
                )
            )
        if prior_counties[vuid] != current_counties[vuid]:
            results.append(
                EarlyVoteVuidDupRow(
                    source_election_id=current.source_election_id,
                    ev_date=current.ev_date,
                    file_id=current.file_id,
                    prior_file_id=prior.file_id,
                    id_voter=vuid,
                    anomaly_kind="county_changed",
                    county=cur_row.county,
                    voting_method=cur_row.voting_method,
                    prior_county=prev_row.county,
                    prior_voting_method=prev_row.voting_method,
                    occurrence_count=1,
                )
            )

    return tuple(results)
