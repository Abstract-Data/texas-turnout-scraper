# integration-tests — Live Integration Tests
# Version: 1.0.0
# Model: claude-sonnet-4-6
# Last Updated: 2026-05-24
# Maintainer: John Eakin

# Prompt 05 — Integration Tests (Live Network)

## Goal
Write `tests/integration/test_civix_live.py` and `tests/integration/test_legacy_live.py` —
integration tests that hit the real APIs. These tests are gated behind a `--live` pytest
marker and are skipped in CI unless the marker is explicitly passed.

## Files to read first (required context)
- `src/texas_turnout_scraper/civix.py`
- `src/texas_turnout_scraper/elections.py`
- `src/texas_turnout_scraper/session.py`
- `src/texas_turnout_scraper/roster.py`
- `src/texas_turnout_scraper/turnout.py`
- `src/texas_turnout_scraper/models.py`
- `src/texas_turnout_scraper/enums.py`
- `src/texas_turnout_scraper/writer.py`
- `tests/conftest.py` (if it exists)

## Files to create
- `tests/integration/test_civix_live.py`
- `tests/integration/test_legacy_live.py`
- `tests/conftest.py` (if it doesn't already define the `--live` marker)
- `tests/integration/__init__.py` (empty, if needed)

## Files NOT to touch
- Any source module under `src/`
- Any unit test

## Pytest marker setup

Add to `tests/conftest.py`:
```python
import pytest

def pytest_addoption(parser):
    parser.addoption("--live", action="store_true", default=False,
                     help="Run integration tests against live APIs")

def pytest_configure(config):
    config.addinivalue_line(
        "markers", "live: mark test as requiring live network access"
    )

def pytest_collection_modifyitems(config, items):
    if not config.getoption("--live"):
        skip_live = pytest.mark.skip(reason="pass --live to run integration tests")
        for item in items:
            if "live" in item.keywords:
                item.add_marker(skip_live)
```

## Civix integration tests (`test_civix_live.py`)

All tests decorated with `@pytest.mark.live`.

### `test_civix_list_elections_returns_results`
```python
@pytest.mark.live
def test_civix_list_elections_returns_results():
    client = CivixClient()
    elections = client.list_elections()
    assert len(elections) > 0
    # All source_election_ids must be strings
    assert all(isinstance(e.source_election_id, str) for e in elections)
    # At least one certified election
    assert any(e.certified for e in elections)
```

### `test_civix_fetch_ev_turnout_returns_counties`
```python
@pytest.mark.live
def test_civix_fetch_ev_turnout_returns_counties():
    client = CivixClient()
    elections = client.list_elections()
    # Find a certified election with at least one EV date
    certified = [e for e in elections if e.certified and e.early_voting_dates]
    if not certified:
        pytest.skip("No certified elections with EV dates available")
    election = certified[0]
    ev_date = election.early_voting_dates[0].date
    turnout = client.fetch_ev_turnout(int(election.source_election_id), ev_date)
    assert len(turnout) > 0
    assert all(isinstance(t.election_id, str) for t in turnout)
```

### `test_civix_fetch_ev_roster_csv_for_small_county`
```python
@pytest.mark.live
def test_civix_fetch_ev_roster_csv_for_small_county():
    # Find a roster-available county in a certified election
    # Prefer LOVING county (smallest) to minimize data transfer
    # Assert returns CountyRoster with records
    # Assert all id_voter values are strings
    # Assert county, election_id, report_date set on every record
    # NEVER print or log id_voter values
```

### `test_civix_voter_record_fields_populated`
```python
@pytest.mark.live
def test_civix_voter_record_fields_populated():
    # Fetch a small roster and assert VoterRecord has all required fields:
    # id_voter (str), voting_method, precinct, county, election_id, report_date
    # voter_name stored (not empty, though we cannot assert its content)
```

## Legacy integration tests (`test_legacy_live.py`)

All tests decorated with `@pytest.mark.live`.

### `test_legacy_list_elections`
```python
@pytest.mark.live
def test_legacy_list_elections():
    # Use LegacySession to fetch election list
    # Assert returns at least 3 LegacyElection objects
    # Assert all source_election_ids are strings
```

### `test_legacy_get_ev_dates`
```python
@pytest.mark.live
def test_legacy_get_ev_dates():
    # Use a known legacy election_id (e.g. "49664")
    # Establish session and get EV dates
    # Assert returns at least 1 LegacyEVDate
    # Assert dates are date objects (not strings)
```

### `test_legacy_fetch_turnout`
```python
@pytest.mark.live
def test_legacy_fetch_turnout():
    # Use a known election + date
    # Assert returns CountyTurnout list with > 0 items
    # Assert STATEWIDE row is excluded
```

### `test_legacy_fetch_roster_strategy_a_small_county`
```python
@pytest.mark.live
def test_legacy_fetch_roster_strategy_a_small_county():
    # Fetch a single county roster (LOVING, county_id="149" is smallest)
    # Assert CountyRoster returned
    # Assert all records have county, election_id, report_date populated
    # NEVER print or log id_voter values
```

## Constraints
- Tests in this file MUST be skipped by default (no `--live` flag)
- No test may use `time.sleep()` directly — pacing is handled by the client
- Never log or print `id_voter` or `voter_name` values
- Strategy A: limit to ONE county per test to avoid long runtimes
- Use `pytest.skip()` when live data doesn't match test preconditions

## Acceptance criteria
```bash
# Without flag — all integration tests skipped
pytest tests/integration/ -v
# 0 errors, N skipped

# With flag — tests run against live APIs (requires network)
pytest tests/integration/ -v --live
# All pass (may be slow due to pacing)
```
