# Testing — texas-turnout-scraper

## Test Layout

```
tests/
├── unit/                              # No network — respx + http_backend="httpx"
│   ├── test_legacy.py                 # Legacy parsers + session
│   ├── test_legacy_http_contract.py   # Struts POST body + prime order
│   ├── test_legacy_strategy_b.py      # Bulk ZIP stream
│   ├── test_legacy_api.py             # legacy_api facades
│   ├── test_http_transport.py         # PacedHttpClient backends
│   ├── test_civix.py
│   ├── test_voterfile.py
│   └── ...
├── integration/                       # Live API tests — require --live
│   ├── test_legacy_live.py
│   ├── test_civix_live.py
│   └── _helpers.py                    # LIVE_HTTP_ERRORS, skip helpers
├── fixtures/
│   ├── early_voting/                  # Legacy SOS HTML/CSV fixtures
│   └── voterfiles/
├── conftest.py                        # --live gate (skips @pytest.mark.live)
└── verify/
    └── check_agents_md.py
```

## Running Tests

```bash
uv sync --dev
uv run ty check
uv run pytest tests/unit -q
uv run pytest tests/integration/ -q              # all live tests skipped
uv run pytest tests/integration/ -v --live        # network + cloudscraper default
uv run pytest tests/verify -q
```

## Test Conventions

### Mocking HTTP with respx

Unit tests use `LegacySession(http_backend="httpx")` and `@respx.mock`. Legacy turnout/roster calls require mocking **both** `getElectionEVDates.do` (session priming) and the data endpoint.

```python
@respx.mock
def test_fetch_turnout():
    respx.post(f"{BASE_URL}/Elections/getElectionEVDates.do").mock(...)
    respx.post(f"{BASE_URL}/Elections/getEVDetails.do").mock(...)
    with LegacySession(http_backend="httpx") as session:
        rows = fetch_turnout(session, "49664", date(2024, 10, 21))
```

### ID type assertions

Always verify IDs remain strings — never int:

```python
assert isinstance(record.id_voter, str)
assert isinstance(election.source_election_id, str)
```

### PII in tests

Fixtures use synthetic names/VUIDs. Tests must not log row contents or assert on PII in log output.

### Live integration gate

`tests/conftest.py` skips tests marked `@pytest.mark.live` unless pytest is invoked with `--live`. Live legacy tests use `LegacySession()` (cloudscraper default).

## Coverage targets

See project prompts and `docs/GUARDRAILS.md`. Unit tests should cover:

- Session establishment and `prime_election` call order
- Turnout HTML parsing and `extract_county_ids` (`townId` values)
- Roster CSV parsing (VUID as str, vote method normalization)
- `legacy_api` facade wiring
- Voterfile DuckDB VUID zero-padding (`lpad`)
