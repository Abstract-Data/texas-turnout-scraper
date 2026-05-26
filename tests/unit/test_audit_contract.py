"""Contract test: audit entry points use canonical finding_type vocabulary."""

from __future__ import annotations

from datetime import date

import pytest

from texas_turnout_scraper.audit import audit_records, audit_roster
from texas_turnout_scraper.enums import FindingType, VoteMethod
from texas_turnout_scraper.models import CountyRoster, VoterRecord

CANONICAL_FINDING_TYPES: frozenset[str] = frozenset(
    {member.value for member in FindingType}
)


def _make_record(
    *,
    vuid: str,
    county: str = "HARRIS",
    method: VoteMethod = VoteMethod.IN_PERSON,
    name: str = "DOE, JANE",
    precinct: str = "0510",
    report_date: date = date(2026, 10, 21),
    election_id: str = "58315",
) -> VoterRecord:
    return VoterRecord(
        id_voter=vuid.zfill(10),
        voter_name=name,
        precinct=precinct,
        voting_method=method,
        county=county,
        election_id=election_id,
        report_date=report_date,
    )


@pytest.mark.parametrize(
    "records, expected_types",
    [
        pytest.param([_make_record(vuid="1")], set(), id="clean_single_record"),
        pytest.param(
            [
                _make_record(vuid="1", county="HARRIS"),
                _make_record(vuid="1", county="TRAVIS"),
            ],
            {FindingType.MULTIPLE_COUNTIES.value},
            id="multiple_counties_finding",
        ),
        pytest.param(
            [
                _make_record(vuid="1", method=VoteMethod.IN_PERSON),
                _make_record(vuid="1", method=VoteMethod.MAIL_IN),
            ],
            {FindingType.CONFLICTING_METHOD.value},
            id="conflicting_method_finding",
        ),
        pytest.param(
            [_make_record(vuid="1"), _make_record(vuid="1")],
            {FindingType.DUPLICATE_VUID.value},
            id="duplicate_vuid_finding",
        ),
    ],
)
def test_audit_roster_emits_canonical_finding_types(records, expected_types):
    roster = CountyRoster(
        county="HARRIS",
        election_id="58315",
        report_date=date(2026, 10, 21),
        source="civix",
        records=records,
    )
    report = audit_roster([roster])
    emitted = {f.finding_type for f in report.findings}
    assert emitted <= CANONICAL_FINDING_TYPES
    assert expected_types <= {f.finding_type for f in report.findings}


@pytest.mark.parametrize(
    "records, expected_types",
    [
        pytest.param([_make_record(vuid="1")], set(), id="clean_single_record"),
        pytest.param(
            [
                _make_record(vuid="1", county="HARRIS"),
                _make_record(vuid="1", county="TRAVIS"),
            ],
            {FindingType.MULTIPLE_COUNTIES.value},
            id="multiple_counties_finding",
        ),
    ],
)
def test_audit_records_emits_canonical_finding_types(records, expected_types):
    report = audit_records(records)
    emitted = {f.finding_type for f in report.findings}
    assert emitted <= CANONICAL_FINDING_TYPES
    assert expected_types <= emitted
    assert report.audit_schema_version == "2.0"
