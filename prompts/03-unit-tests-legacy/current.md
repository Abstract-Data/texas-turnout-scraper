# unit-tests-legacy — Unit Tests: Legacy SOS Modules
# Version: 1.0.0
# Model: claude-sonnet-4-6
# Last Updated: 2026-05-24
# Maintainer: John Eakin

# Prompt 03 — Unit Tests: Legacy SOS Modules (respx-mocked)

## Goal
Write `tests/unit/test_legacy.py` — full unit test coverage for the four legacy
SOS modules: `elections.py`, `roster.py`, `turnout.py`, and `session.py`.
Use `respx` to mock all HTTP calls. No live network requests.

## Files to read first (required context)
- `src/texas_turnout_scraper/elections.py`
- `src/texas_turnout_scraper/roster.py`
- `src/texas_turnout_scraper/turnout.py`
- `src/texas_turnout_scraper/session.py`
- `src/texas_turnout_scraper/models.py`
- `src/texas_turnout_scraper/enums.py`
- `tests/fixtures/early_voting/legacy_election_index.html`
- `tests/fixtures/early_voting/legacy_ev_dates_49664.html`
- `tests/fixtures/early_voting/legacy_ev_details_49664.html`
- `tests/fixtures/early_voting/legacy_voter_info_loving.csv`

## Files to create
- `tests/unit/test_legacy.py`

## Files NOT to touch
- Any source module under `src/`
- Any fixture file

## Important: VoterRecord fields

`VoterRecord` requires these fields — all must be present on records returned
by `roster.py`'s `_parse_county_csv()`:
- `id_voter: str`
- `voting_method: VoteMethod`
- `precinct: str`
- `county: str` — comes from the `county_name` parameter in `_parse_county_csv`
- `election_id: str`
- `report_date: date`
- `voter_name: str` — stored for mismatch detection, never logged

## Test structure

### `elections.py` tests

#### `test_list_elections_parses_dropdown`
```python
def test_list_elections_parses_dropdown():
    # Load legacy_election_index.html fixture
    # Call list_elections(html) (if it accepts HTML string)
    # OR mock the HTTP GET and call through LegacySession
    # Assert returns 3 LegacyElection objects
    # Assert first: source_election_id=="49664", election_name contains "GENERAL"
    # Assert source_election_id is always str
```

#### `test_get_ev_dates_parses_date_dropdown`
```python
def test_get_ev_dates_parses_date_dropdown():
    # Load legacy_ev_dates_49664.html fixture
    # Call get_ev_dates(html) or mock HTTP POST
    # Assert returns 3 LegacyEVDate objects
    # Assert dates: [date(2024,10,21), date(2024,10,22), date(2024,11,1)]
    # (The "00:00:00.0" suffix in option values must be stripped)
```

#### `test_election_type_inferred_from_name`
```python
def test_election_type_inferred_from_name():
    # 2024 NOVEMBER 5TH GENERAL ELECTION → ElectionType.GENERAL
    # 2024 MARCH 5TH REPUBLICAN PRIMARY → ElectionType.PRIMARY
    # 2024 MARCH 5TH DEMOCRATIC PRIMARY → ElectionType.PRIMARY
```

### `turnout.py` tests

#### `test_fetch_turnout_parses_county_table`
```python
@respx.mock
def test_fetch_turnout_parses_county_table():
    # Load legacy_ev_details_49664.html fixture
    # Mock POST to /Elections/getEVDetails.do
    # Call fetch_turnout(session, "49664", date(2024,10,21))
    # Assert returns 3 CountyTurnout objects (LOVING, HARRIS, TRAVIS — not STATEWIDE)
    # Assert LOVING: registered_voters==100, in_person_votes_on_date==2
    # Assert HARRIS: total_in_person_votes==88500, total_mail_votes==1200
```

#### `test_extract_county_ids_from_html`
```python
def test_extract_county_ids_from_html():
    # Load legacy_ev_details_49664.html fixture
    # Call extract_county_ids(html)
    # Assert returns ["149", "101", "227"] (from onclick="downloadReport('...')")
    # STATEWIDE row has no onclick — must be excluded
```

#### `test_statewide_row_excluded_from_turnout`
```python
def test_statewide_row_excluded_from_turnout():
    # Same as fetch_turnout test but specifically assert
    # no CountyTurnout with county=="STATEWIDE" is returned
```

### `roster.py` tests

#### `test_parse_county_csv_returns_voter_records`
```python
def test_parse_county_csv_returns_voter_records():
    # Load legacy_voter_info_loving.csv fixture as text
    # Call _parse_county_csv(raw_text, county_id="149", county_name="LOVING",
    #                        source_election_id="49664", ev_date=date(2024,10,21))
    # Assert returns CountyRoster with 6 VoterRecord objects
    # Assert all id_voter values are strings
    # Assert third record voting_method == VoteMethod.MAIL_IN
    # Assert all records have county=="LOVING"
    # Assert all records have election_id=="49664"
    # Assert all records have report_date==date(2024,10,21)
```

#### `test_parse_county_csv_voter_name_stored`
```python
def test_parse_county_csv_voter_name_stored():
    # Same fixture
    # Assert first record voter_name == "DOE, LOVING A"
    # voter_name is stored (not discarded) for name-mismatch detection
    # Do NOT print or log voter_name in the test
```

#### `test_parse_county_csv_id_voter_always_string`
```python
def test_parse_county_csv_id_voter_always_string():
    # Same fixture
    # Assert all records: isinstance(r.id_voter, str)
    # Assert first VUID: "2000000001" (not int)
```

#### `test_parse_county_csv_empty_returns_none`
```python
def test_parse_county_csv_empty_returns_none():
    # Call _parse_county_csv with empty string or header-only CSV
    # Assert returns None
```

#### `test_parse_county_csv_malformed_skips_rows`
```python
def test_parse_county_csv_malformed_skips_rows():
    # CSV with one good row and one row with missing ID_VOTER
    # Assert returns CountyRoster with only 1 record
```

#### `test_fetch_roster_strategy_a_with_county_names`
```python
@respx.mock
def test_fetch_roster_strategy_a_with_county_names():
    # Mock POST to /Elections/getEVReport.do returning Loving fixture CSV
    # Call fetch_roster_strategy_a(
    #     session, "49664", date(2024,10,21), ["149"],
    #     county_names={"149": "LOVING"}
    # )
    # Assert returns 1 CountyRoster with county=="LOVING"
    # Assert all records have county=="LOVING"
```

#### `test_fetch_roster_strategy_a_fallback_county_name`
```python
@respx.mock
def test_fetch_roster_strategy_a_fallback_county_name():
    # Same mock but call WITHOUT county_names
    # Assert county falls back to "COUNTY_149"
```

### `session.py` tests

#### `test_legacy_session_establishes_cookie`
```python
@respx.mock
def test_legacy_session_establishes_cookie():
    # Mock GET to /Elections/getElectionDetails.do → sets Set-Cookie: JSESSIONID=abc123
    # Call LegacySession.establish("49664")
    # Assert session._client.cookies["JSESSIONID"] == "abc123"
    # OR assert that subsequent requests carry the cookie
```

#### `test_legacy_session_pace_enforced`
```python
def test_legacy_session_pace_enforced():
    # Create LegacySession with pace_seconds=0.1
    # Time two consecutive _pace() calls
    # Assert elapsed >= 0.1 s
```

## Constraints
- No live HTTP requests — all requests must be intercepted by respx
- Never assert on VUID values in error messages or test output (PII guard)
- Always assert `isinstance(r.id_voter, str)` for every VoterRecord test
- Always assert `county`, `election_id`, `report_date` are set correctly
- Import fixtures using `pathlib.Path(__file__).parent.parent / "fixtures" / "early_voting" / ...`
- Access `_parse_county_csv` directly for internal-helper tests

## Acceptance criteria
```bash
pytest tests/unit/test_legacy.py -v
# All tests pass, no live HTTP calls made
```
