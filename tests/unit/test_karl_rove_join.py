"""Unit tests for Karl Rove join and aggregation (no live APIs)."""

from __future__ import annotations

import csv
import io
import json
import logging

import pytest

from reports.karl_rove.ev_roster import _parse_roster_vuids
from reports.karl_rove.join import (
    assemble_report,
    build_cohort_rows,
    build_registration_year_rows,
    vep_years_from_api,
)
from reports.karl_rove.models import VepYearCount, VepYearsSummary
from reports.karl_rove.ndjson import parse_ndjson_line
from reports.karl_rove.render import render_html
from reports.karl_rove.vep import VepClientError
from reports.karl_rove.vep_index import VepMatchIndex
from reports.karl_rove.vuid import normalize_vuid

_SECRET_NAME = "ZaphodBeeblebrox"
_SECRET_ZIP = "90210"


def test_normalize_vuid_zfill() -> None:
    assert normalize_vuid("12345") == "0000012345"


def test_vep_years_from_api_shape() -> None:
    summary = vep_years_from_api({"years": [{"vep_year": 2026, "matches": 100}], "total": 100})
    assert summary.total == 100
    assert summary.years[0].vep_cohort_year == 2026


def test_build_cohort_rows_percent() -> None:
    vep = VepYearsSummary(years=(VepYearCount(vep_cohort_year=2026, registrants=100),), total=100)
    cohort = {2026: frozenset({"0000000001", "0000000002", "0000000003"})}
    early = frozenset({"0000000001", "0000000002"})
    rows, total = build_cohort_rows(vep, cohort, early)
    assert rows[0].early_voted == 2
    assert rows[0].percent_early_voted == 2.0
    assert total.early_voted == 2


def test_registration_year_totals_reconcile_with_cohort() -> None:
    """Unknown, future, and normal registration years all count toward the same total."""
    vep = VepYearsSummary(
        years=(VepYearCount(vep_cohort_year=2026, registrants=5),),
        total=5,
    )
    cohort = {2026: frozenset({f"{i:010d}" for i in range(1, 6)})}
    registration = {
        2025: frozenset({"0000000001", "0000000002"}),
        2027: frozenset({"0000000003"}),
        None: frozenset({"0000000004", "0000000005"}),
    }
    early = frozenset({"0000000001", "0000000004"})
    report = assemble_report(
        source_election_id="53815",
        election_label="Test",
        as_of_ev_date="2026-10-24",
        generated_at_iso="2026-10-27T00:00:00+00:00",
        vep_years=vep,
        cohort_vuids=cohort,
        registration_year_vuids=registration,
        early_voted_vuids=early,
    )
    assert report.cohort_early_vote_total.registrants == 5
    assert report.registration_year_early_vote_total.registrants == 5
    cohort_total = report.cohort_early_vote_total.registrants
    reg_total = report.registration_year_early_vote_total.registrants
    assert cohort_total == reg_total
    assert sum(r.registrants for r in report.registration_year_early_vote) == 5
    labels = {r.label for r in report.registration_year_early_vote}
    assert "Unknown" in labels
    assert any(r.future_year_data_quality for r in report.registration_year_early_vote)


def test_build_registration_year_rows_future_flag() -> None:
    rows, total = build_registration_year_rows(
        {2027: frozenset({"0000000001"})},
        frozenset(),
        reference_year=2026,
    )
    assert rows[0].future_year_data_quality is True
    assert total.registrants == 1


def test_assemble_report_multi_cohort() -> None:
    vep = VepYearsSummary(
        years=(
            VepYearCount(vep_cohort_year=2025, registrants=10),
            VepYearCount(vep_cohort_year=2026, registrants=20),
        ),
        total=30,
    )
    cohort = {
        2025: frozenset({"0000000001"}),
        2026: frozenset({"0000000002"}),
    }
    registration = {
        2024: frozenset({"0000000001"}),
        2025: frozenset({"0000000002"}),
    }
    early = frozenset({"0000000001"})
    report = assemble_report(
        source_election_id="53815",
        election_label="Test",
        as_of_ev_date="2026-10-24",
        generated_at_iso="2026-10-27T00:00:00+00:00",
        vep_years=vep,
        cohort_vuids=cohort,
        registration_year_vuids=registration,
        early_voted_vuids=early,
    )
    assert len(report.cohort_early_vote) == 2
    assert report.cohort_early_vote_total.early_voted == 1


def test_parse_roster_vuids_distinct() -> None:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["ID_VOTER", "VOTING_METHOD"])
    writer.writerow(["1", "IN-PERSON"])
    writer.writerow(["1", "IN-PERSON"])
    writer.writerow(["2", "MAIL-IN"])
    vuids = _parse_roster_vuids(buf.getvalue().encode())
    assert vuids == frozenset({"0000000001", "0000000002"})


def test_ndjson_drops_pii_fields_from_output_and_logs(caplog: pytest.LogCaptureFixture) -> None:
    line = json.dumps(
        {
            "vep_year": 2026,
            "Voter_voter_id": "99",
            "Voter_registration_date": "2025-06-01",
            "Voter_first_name": _SECRET_NAME,
            "Voter_last_name": "Adams",
            "Zip": _SECRET_ZIP,
            "County": "Travis",
            "prospect_id": "p-999",
        }
    ).encode()
    with caplog.at_level(logging.DEBUG):
        row = parse_ndjson_line(line)
        assert row is not None
        index = VepMatchIndex()
        index.ingest(row)
        report = assemble_report(
            source_election_id="53815",
            election_label="X",
            as_of_ev_date="2026-10-24",
            generated_at_iso="2026-10-27T00:00:00+00:00",
            vep_years=VepYearsSummary(
                years=(VepYearCount(vep_cohort_year=2026, registrants=1),),
                total=1,
            ),
            cohort_vuids=index.cohort_frozen(),
            registration_year_vuids=index.registration_frozen(),
            early_voted_vuids=frozenset({"0000000099"}),
        )
        html = render_html(report)
        model_dump = report.model_dump_json()
    for blob in (html, model_dump, caplog.text):
        assert _SECRET_NAME not in blob
        assert _SECRET_ZIP not in blob
        assert "Adams" not in blob
        assert "Travis" not in blob
        assert "p-999" not in blob


def test_vep_client_error_masks_body() -> None:
    err = VepClientError(path="/v1/matches.ndjson", status_code=403)
    assert "403" in str(err)
    assert "body" not in str(err).lower()


def test_render_html_fixture_banner() -> None:
    vep = VepYearsSummary(years=(VepYearCount(vep_cohort_year=2026, registrants=1),), total=1)
    report = assemble_report(
        source_election_id="53815",
        election_label="X",
        as_of_ev_date="2026-10-24",
        generated_at_iso="2026-10-27T00:00:00+00:00",
        vep_years=vep,
        cohort_vuids={2026: frozenset()},
        registration_year_vuids={None: frozenset()},
        early_voted_vuids=frozenset(),
        is_fixture=True,
    )
    html = render_html(report)
    assert "EXAMPLE DATA" in html
    assert "VEP cohort year" in html
    assert "registration year" in html.lower()
