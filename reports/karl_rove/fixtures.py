"""EXAMPLE fixture data for CI and screenshots (no names or row-level PII)."""

from __future__ import annotations

from datetime import datetime, timezone

from reports.karl_rove.models import (
    CohortEvRow,
    KarlRoveReportData,
    RegistrationYearEvRow,
    VepYearCount,
    VepYearsSummary,
)


def example_report() -> KarlRoveReportData:
    """EXAMPLE headline numbers; join math is covered in unit tests with small sets."""
    vep_years = VepYearsSummary(
        years=(VepYearCount(vep_cohort_year=2026, registrants=228_265),),
        total=228_265,
    )
    cohort_row = CohortEvRow(
        vep_cohort_year=2026,
        registrants=228_265,
        early_voted=41_157,
        percent_early_voted=18.0,
    )
    cohort_total = CohortEvRow(
        vep_cohort_year=0,
        registrants=228_265,
        early_voted=41_157,
        percent_early_voted=18.0,
    )
    reg_rows = (
        RegistrationYearEvRow(
            label="2025",
            registration_year=2025,
            registrants=200_000,
            early_voted=36_000,
            percent_early_voted=18.0,
        ),
        RegistrationYearEvRow(
            label="Unknown",
            registration_year=None,
            registrants=20_000,
            early_voted=3_657,
            percent_early_voted=18.3,
        ),
        RegistrationYearEvRow(
            label="2027",
            registration_year=2027,
            registrants=8_265,
            early_voted=1_500,
            percent_early_voted=18.1,
            future_year_data_quality=True,
        ),
    )
    reg_total = RegistrationYearEvRow(
        label="Total",
        registration_year=None,
        registrants=228_265,
        early_voted=41_157,
        percent_early_voted=18.0,
    )
    return KarlRoveReportData(
        source_election_id="53815",
        election_label="2026 General Election",
        as_of_ev_date="2026-10-24",
        generated_at_iso=datetime.now(tz=timezone.utc).replace(microsecond=0).isoformat(),
        vep_registrations=vep_years,
        cohort_early_vote=(cohort_row,),
        cohort_early_vote_total=cohort_total,
        registration_year_early_vote=reg_rows,
        registration_year_early_vote_total=reg_total,
        is_fixture=True,
    )
