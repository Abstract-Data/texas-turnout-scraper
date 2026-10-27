"""Join VEP cohort sets with early-vote VUID set; output counts only."""

from __future__ import annotations

from collections.abc import Mapping

from reports.karl_rove.models import (
    CohortEvRow,
    KarlRoveReportData,
    RegistrationYearEvRow,
    VepYearCount,
    VepYearsSummary,
)


def _percent(early_voted: int, registrants: int) -> float:
    if registrants <= 0:
        return 0.0
    return round(100.0 * early_voted / registrants, 1)


def build_cohort_rows(
    vep_years: VepYearsSummary,
    cohort_vuids: Mapping[int, frozenset[str]],
    early_voted_vuids: frozenset[str],
) -> tuple[tuple[CohortEvRow, ...], CohortEvRow]:
    rows: list[CohortEvRow] = []
    total_reg = 0
    total_ev = 0
    for year_row in sorted(vep_years.years, key=lambda y: y.vep_cohort_year):
        year = year_row.vep_cohort_year
        registrants = year_row.registrants
        vuids = cohort_vuids.get(year, frozenset())
        early_voted = sum(1 for v in vuids if v in early_voted_vuids)
        rows.append(
            CohortEvRow(
                vep_cohort_year=year,
                registrants=registrants,
                early_voted=early_voted,
                percent_early_voted=_percent(early_voted, registrants),
            )
        )
        total_reg += registrants
        total_ev += early_voted
    total = CohortEvRow(
        vep_cohort_year=0,
        registrants=total_reg,
        early_voted=total_ev,
        percent_early_voted=_percent(total_ev, total_reg),
    )
    return tuple(rows), total


def _registration_label(year: int | None) -> str:
    if year is None:
        return "Unknown"
    return str(year)


def build_registration_year_rows(
    registration_year_vuids: Mapping[int | None, frozenset[str]],
    early_voted_vuids: frozenset[str],
    *,
    reference_year: int,
) -> tuple[tuple[RegistrationYearEvRow, ...], RegistrationYearEvRow]:
    known_years = sorted(y for y in registration_year_vuids if y is not None)
    order: list[int | None] = list(known_years)
    if None in registration_year_vuids:
        order.append(None)
    rows: list[RegistrationYearEvRow] = []
    total_reg = 0
    total_ev = 0
    for reg_year in order:
        vuids = registration_year_vuids.get(reg_year, frozenset())
        registrants = len(vuids)
        early_voted = sum(1 for v in vuids if v in early_voted_vuids)
        future = reg_year is not None and reg_year > reference_year
        rows.append(
            RegistrationYearEvRow(
                label=_registration_label(reg_year),
                registration_year=reg_year,
                registrants=registrants,
                early_voted=early_voted,
                percent_early_voted=_percent(early_voted, registrants),
                future_year_data_quality=future,
            )
        )
        total_reg += registrants
        total_ev += early_voted
    total = RegistrationYearEvRow(
        label="Total",
        registration_year=None,
        registrants=total_reg,
        early_voted=total_ev,
        percent_early_voted=_percent(total_ev, total_reg),
        future_year_data_quality=False,
    )
    return tuple(rows), total


def reference_year_from_ev_date(as_of_ev_date: str) -> int:
    prefix = as_of_ev_date.strip()[:4]
    if len(prefix) == 4 and prefix.isdigit():
        return int(prefix)
    return 0


def vep_years_from_api(payload: dict[str, object]) -> VepYearsSummary:
    raw_years = payload.get("years")
    if not isinstance(raw_years, list):
        msg = "years missing from VEP /v1/years response"
        raise ValueError(msg)
    years: list[VepYearCount] = []
    for item in raw_years:
        if not isinstance(item, dict):
            continue
        year = item.get("vep_year")
        matches = item.get("matches")
        if year is None or matches is None:
            continue
        years.append(VepYearCount(vep_cohort_year=int(year), registrants=int(matches)))
    total_raw = payload.get("total")
    total = int(total_raw) if total_raw is not None else sum(y.registrants for y in years)
    return VepYearsSummary(years=tuple(sorted(years, key=lambda y: y.vep_cohort_year)), total=total)


def assemble_report(
    *,
    source_election_id: str,
    election_label: str,
    as_of_ev_date: str,
    generated_at_iso: str,
    vep_years: VepYearsSummary,
    cohort_vuids: Mapping[int, frozenset[str]],
    registration_year_vuids: Mapping[int | None, frozenset[str]],
    early_voted_vuids: frozenset[str],
    is_fixture: bool = False,
) -> KarlRoveReportData:
    cohort_rows, cohort_total = build_cohort_rows(vep_years, cohort_vuids, early_voted_vuids)
    reg_rows, reg_total = build_registration_year_rows(
        registration_year_vuids,
        early_voted_vuids,
        reference_year=reference_year_from_ev_date(as_of_ev_date),
    )
    return KarlRoveReportData(
        source_election_id=source_election_id,
        election_label=election_label,
        as_of_ev_date=as_of_ev_date,
        generated_at_iso=generated_at_iso,
        vep_registrations=vep_years,
        cohort_early_vote=cohort_rows,
        cohort_early_vote_total=cohort_total,
        registration_year_early_vote=reg_rows,
        registration_year_early_vote_total=reg_total,
        is_fixture=is_fixture,
    )
