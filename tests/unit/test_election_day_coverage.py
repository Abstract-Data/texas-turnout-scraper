"""Issue #151: an Election Day roster missing counties is flagged, never blocked."""

from __future__ import annotations

import respx

from tests.unit.test_election_day_roster import (
    CANONICAL_ROWS,
    EARLY_DAY,
    EARLY_HEADER,
    ED_HEADER,
    ELECTION_DAY,
    GET_FILE,
    ROWS,
    TODAY,
    _ed_zip,
    _envelope,
    _KeepingStore,
    _mock_election_day,
)
from texas_turnout_scraper.early_vote_backfill import (
    DEFAULT_NOV_2026_GENERAL_ID,
    BackfillResult,
    format_backfill_report,
    plan_backfill_load_dates,
    run_backfill,
)
from texas_turnout_scraper.early_vote_roster import (
    CountyShortfall,
    canonicalize_roster_csv,
    low_coverage_counties,
)

EARLY_COUNTS = {"BIGCO": 150, "OTHERCO": 200, "SMALLCO": 50}


def _rows(counts: dict[str, int]) -> str:
    return "".join(
        f'"{county}","DOE, {n}","{n:010d}","IN-PERSON","1"\n'
        for county, total in counts.items()
        for n in range(total)
    )


def _mock_early_counts(router: respx.MockRouter, counts: dict[str, int]) -> None:
    router.get(GET_FILE, params={"type": "EVR_STATEWIDE", "electionDate": "10/14/2026"}).mock(
        return_value=_envelope(f"{EARLY_HEADER}\n{_rows(counts)}".encode())
    )


def _run(*, election_day: bool = True) -> tuple[_KeepingStore, BackfillResult]:
    store = _KeepingStore()
    plan = plan_backfill_load_dates(
        DEFAULT_NOV_2026_GENERAL_ID,
        (EARLY_DAY,),
        today=TODAY,
        election_day=ELECTION_DAY if election_day else None,
    )
    return store, run_backfill(DEFAULT_NOV_2026_GENERAL_ID, store, plan)


def test_a_county_under_the_share_is_flagged_with_its_rounded_baseline() -> None:
    flagged = low_coverage_counties({"BIGCO": 9}, {"BIGCO": 100.4})

    assert flagged == (CountyShortfall(county="BIGCO", rows=9, baseline_rows=100),)


def test_a_county_exactly_at_the_share_is_not_flagged() -> None:
    assert low_coverage_counties({"BIGCO": 10}, {"BIGCO": 100.0}) == ()


def test_a_county_with_a_small_baseline_is_never_flagged() -> None:
    assert low_coverage_counties({}, {"SMALLCO": 99.9}) == ()


def test_a_county_absent_from_the_day_counts_as_zero_rows() -> None:
    flagged = low_coverage_counties({"BIGCO": 100}, {"GONE": 300.0, "BIGCO": 100.0})

    assert flagged == (CountyShortfall(county="GONE", rows=0, baseline_rows=300),)


def test_flagged_counties_are_sorted_and_healthy_counties_return_nothing() -> None:
    day = {"B": 1, "A": 1, "C": 500}
    baseline = {"B": 200.0, "A": 200.0, "C": 500.0}

    assert [item.county for item in low_coverage_counties(day, baseline)] == ["A", "B"]
    assert low_coverage_counties({"A": 200, "B": 200}, {"A": 200.0, "B": 200.0}) == ()


def test_county_counts_tally_trimmed_counties_and_skip_empty_cells() -> None:
    csv_text = f'{EARLY_HEADER}\n{ROWS}" TRAVIS ","DOE, THREE",0000000003,IN-PERSON,1\n,x,y,z,1\n'

    _, summary = canonicalize_roster_csv(csv_text.encode())

    assert summary.county_counts == (("BEXAR", 1), ("TRAVIS", 2))


def test_county_name_counts_under_the_rename_without_changing_bytes() -> None:
    canonical, summary = canonicalize_roster_csv(f"{ED_HEADER}\n{ROWS}".encode())

    assert summary.county_counts == (("BEXAR", 1), ("TRAVIS", 1))
    assert canonical == f"{EARLY_HEADER}\n{CANONICAL_ROWS}".encode()


def test_a_file_without_a_county_column_has_no_county_counts() -> None:
    _, summary = canonicalize_roster_csv(b'VOTER_NAME,ID_VOTER\n"DOE, ONE",0000000001\n')

    assert summary.county_counts == ()
    assert canonicalize_roster_csv(b"")[1].county_counts == ()


@respx.mock
def test_a_collapsed_county_is_flagged_but_the_day_is_still_written() -> None:
    _mock_early_counts(respx.mock, EARLY_COUNTS)
    _mock_election_day(
        respx.mock, _envelope(_ed_zip(rows=_rows({"BIGCO": 5, "OTHERCO": 200, "SMALLCO": 1})))
    )

    store, result = _run()

    assert result.coverage_checked is True
    assert result.low_coverage_counties == (
        CountyShortfall(county="BIGCO", rows=5, baseline_rows=150),
    )
    assert "2026-11-03" in store.written
    assert result.exit_code == 0
    report = format_backfill_report(result)
    assert "coverage_checked=true" in report
    assert "low_coverage_count=1" in report
    assert "low_coverage_counties=BIGCO:5/150" in report


@respx.mock
def test_the_report_appends_coverage_keys_after_the_existing_ones() -> None:
    _mock_early_counts(respx.mock, EARLY_COUNTS)
    _mock_election_day(respx.mock, _envelope(_ed_zip(rows=_rows(EARLY_COUNTS))))

    _, result = _run()

    keys = [line.split("=", 1)[0] for line in format_backfill_report(result).splitlines()]
    assert keys.index("exit_code") < keys.index("kv_bump") < keys.index("coverage_checked")
    assert keys.index("low_coverage_counties") < keys.index("day ev_date")


@respx.mock
def test_a_healthy_election_day_is_checked_and_flags_nothing() -> None:
    _mock_early_counts(respx.mock, EARLY_COUNTS)
    _mock_election_day(respx.mock, _envelope(_ed_zip(rows=_rows(EARLY_COUNTS))))

    _, result = _run()

    assert result.coverage_checked is True
    assert result.low_coverage_counties == ()
    report = format_backfill_report(result)
    assert "low_coverage_count=0" in report
    assert "low_coverage_counties=\n" in report + "\n"


@respx.mock
def test_an_early_only_plan_is_not_checked() -> None:
    _mock_early_counts(respx.mock, EARLY_COUNTS)

    _, result = _run(election_day=False)

    assert result.coverage_checked is False
    assert result.low_coverage_counties == ()
    assert "coverage_checked=false" in format_backfill_report(result)


@respx.mock
def test_an_election_day_without_an_early_day_loaded_is_not_checked() -> None:
    _mock_early_counts(respx.mock, {})
    _mock_election_day(respx.mock, _envelope(_ed_zip(rows=_rows({"BIGCO": 5}))))

    store, result = _run()

    assert result.skipped_empty_dates == ("2026-10-14",)
    assert "2026-11-03" in store.written
    assert result.coverage_checked is False
    assert result.low_coverage_counties == ()
    assert "coverage_checked=false" in format_backfill_report(result)
