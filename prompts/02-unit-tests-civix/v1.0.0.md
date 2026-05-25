# unit-tests-civix — Unit Tests: civix.py
# Version: 1.0.0
# Model: claude-sonnet-4-6
# Last Updated: 2026-05-24
# Maintainer: John Eakin

# Prompt 02 — Unit Tests: civix.py (respx-mocked)

## Goal
Write `tests/unit/test_civix.py` — full unit test coverage for `civix.py` using
`respx` to mock all HTTP calls. No live network requests.

## Files to read first (required context)
- `src/texas_turnout_scraper/civix.py`
- `src/texas_turnout_scraper/models.py`
- `src/texas_turnout_scraper/enums.py`
- `tests/fixtures/early_voting/civix_election_index.json`
- `tests/fixtures/early_voting/civix_earlyvoting_53813.json`
- `tests/fixtures/early_voting/civix_roster_harris_sample.csv`
- `tests/fixtures/early_voting/civix_ed_turnout_53813.json`

## Files to create
- `tests/unit/test_civix.py`

## Files NOT to touch
- Any source module under `src/`
- Any fixture file

## Key pattern — respx mocking for the base64 envelope

The Civix API wraps every response in `{"upload": "<base64>"}`.
When mocking, you must return a properly wrapped response:

```python
import base64, json, respx, httpx

def _civix_json_response(data: dict) -> httpx.Response:
    """Wrap a dict in the Civix base64 envelope."""
    encoded = base64.b64encode(json.dumps(data).encode()).decode()
    return httpx.Response(200, json={"upload": encoded})

def _civix_csv_response(csv_text: str) -> httpx.Response:
    """Wrap CSV text in the Civix base64 envelope."""
    encoded = base64.b64encode(csv_text.encode()).decode()
    return httpx.Response(200, json={"upload": encoded})

def _civix_zip_response(zip_bytes: bytes) -> httpx.Response:
    """Wrap ZIP bytes in the Civix base64 envelope."""
    encoded = base64.b64encode(zip_bytes).decode()
    return httpx.Response(200, json={"upload": encoded})
```

## VoterRecord fields (updated model — read carefully)

`VoterRecord` now has these REQUIRED fields (all must be passed):
- `id_voter: str` — 10-digit VUID string
- `voting_method: VoteMethod`
- `precinct: str`
- `county: str` — all-caps county name
- `election_id: str` — source_election_id as string
- `report_date: date` — the EV date this record came from

And these optional fields (populated by `accumulate_roster()`, not the fetch functions):
- `voter_name: str = ""` — stored for mismatch detection, never logged
- `duplicate_flag: bool = False`
- `duplicate_type: str = ""`
- `also_found_on: str = ""`

When asserting on VoterRecord objects returned by `fetch_ev_roster_csv()` or
`fetch_ed_roster_zip()`, always verify the required fields are populated correctly.

## Test structure

Use `pytest` + `respx`. All tests are synchronous (CivixClient is sync).

### `test_list_elections_parses_correctly`
```python
@respx.mock
def test_list_elections_parses_correctly():
    # Load civix_election_index.json fixture
    # Mock GET to /api-ivis-system/api/v1/getFile?type=EVR_ELECTION
    # Call CivixClient().list_elections()
    # Assert:
    #   - returns list of CivixElection
    #   - first election source_election_id == "53813" (str, not int)
    #   - first election election_type == ElectionType.PRIMARY
    #   - first election certified == True
    #   - len(early_voting_dates) == 3
    #   - len(counties) == 3
    #   - second election election_type == ElectionType.SPECIAL
```

### `test_list_elections_source_election_id_is_string`
```python
@respx.mock
def test_list_elections_source_election_id_is_string():
    # Same mock
    # Assert: all elections have isinstance(e.source_election_id, str)
```

### `test_fetch_ev_turnout_parses_correctly`
```python
@respx.mock
def test_fetch_ev_turnout_parses_correctly():
    # Load civix_earlyvoting_53813.json fixture
    # Mock GET to /getFile?type=EVR_EARLYVOTING
    # Call fetch_ev_turnout(53813, date(2026, 2, 27))
    # Assert:
    #   - returns 3 CivixCountyTurnout objects
    #   - ANDERSON: registered_voters=30678, in_person_votes_on_date=707
    #   - HARRIS: roster_available=True (voter_details_report is a string)
    #   - TRAVIS: roster_available=False (voter_details_report is False bool)
    #   - All election_id values are strings ("53813")
```

### `test_fetch_ev_turnout_roster_available_logic`
```python
# Test the three voter_details_report states:
# 1. string value → roster_available=True
# 2. True (bool) → roster_available=True
# 3. False (bool) → roster_available=False
```

### `test_fetch_ev_roster_csv_returns_voter_records`
```python
@respx.mock
def test_fetch_ev_roster_csv_returns_voter_records():
    # Load civix_roster_harris_sample.csv fixture as text
    # Wrap in base64 envelope
    # Mock GET to /getFileByFormat?type=EVR_EARLYVOTING&format=csv
    # Call fetch_ev_roster_csv(53813, date(2026, 2, 27), "HARRIS", 101)
    # Assert:
    #   - returns 10 VoterRecord objects
    #   - all id_voter values are strings
    #   - first record: voting_method == VoteMethod.IN_PERSON
    #   - third record: voting_method == VoteMethod.MAIL_IN
    #   - all records have county == "HARRIS"
    #   - all records have election_id == "53813"
    #   - all records have report_date == date(2026, 2, 27)
```

### `test_fetch_ev_roster_csv_id_voter_is_always_string`
```python
@respx.mock
def test_fetch_ev_roster_csv_id_voter_is_always_string():
    # Same mock
    # Assert: all records have isinstance(r.id_voter, str)
    # Assert: leading zeros preserved — "0000000001" not 1
```

### `test_fetch_ev_roster_csv_voter_name_stored`
```python
@respx.mock
def test_fetch_ev_roster_csv_voter_name_stored():
    # Same mock
    # Assert: first record voter_name == "DOE, JOHN A"
    # (voter_name is stored for mismatch detection, not discarded)
    # Do NOT log or print voter_name in test output
```

### `test_fetch_ed_turnout_voter_details_report_true_is_roster_available`
```python
@respx.mock
def test_fetch_ed_turnout_voter_details_report_true_is_roster_available():
    # Load civix_ed_turnout_53813.json fixture
    # Mock GET to /getFile?type=EVR_ELECTIONDAYTURNOUT
    # All counties have voter_details_report=True (bool)
    # Assert all counties have roster_available=True
```

### `test_fetch_ed_roster_zip_parses_all_csvs_in_zip`
```python
@respx.mock
def test_fetch_ed_roster_zip_parses_all_csvs_in_zip():
    # Build a real in-memory ZIP with one CSV file containing 3 voter rows
    # Use the same CSV header: "VOTER_NAME","ID_VOTER","VOTING_METHOD","PRECINCT"
    # Wrap ZIP bytes in base64 envelope
    # Mock GET to /getFileByFormat?type=EVR_ELECTIONDAYTURNOUT&format=zip
    # Call fetch_ed_roster_zip(53813, date(2026, 3, 3), "HARRIS", 101)
    # Assert returns 3 VoterRecord objects
    # Assert all records have county == "HARRIS", election_id == "53813"
    # Assert all records have report_date == date(2026, 3, 3)
```

### `test_fetch_county_roster_convenience_function`
```python
@respx.mock
def test_fetch_county_roster_convenience_function():
    # Test the module-level fetch_county_roster() function
    # Mock CSV endpoint
    # Assert returns CountyRoster with correct county, election_id, source="civix"
    # Assert total_voters == len(records)
    # Assert all records have county and election_id populated
```

### `test_pacing_enforced` (optional but valuable)
```python
def test_pacing_enforced():
    # Create CivixClient(pace_seconds=0.1)
    # Time two consecutive mock requests
    # Assert total elapsed >= 0.1 s
```

### `test_http_error_raises`
```python
@respx.mock
def test_http_error_raises():
    # Mock GET to return 502
    # Assert client.list_elections() raises httpx.HTTPStatusError
```

## Constraints
- No live HTTP requests — all requests must be intercepted by respx
- Never assert on actual VUID values in error messages (PII guard)
- Test `source_election_id` is `str` in every test that returns elections
- Test `county`, `election_id`, `report_date` are populated on VoterRecord in every roster test
- Import fixtures using `pathlib.Path(__file__).parent.parent / "fixtures" / "early_voting" / ...`

## Acceptance criteria
```bash
pytest tests/unit/test_civix.py -v
# All tests pass, no live HTTP calls made
```
