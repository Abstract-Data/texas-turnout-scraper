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

## Property and stateful tests (Hypothesis)

`tests/unit/property/` holds Hypothesis tests for the places where edge cases hide. Every strategy generates synthetic data only, so a printed failing example never holds a real voter.

| File | What it covers |
|---|---|
| `test_validation_properties.py` | Record validation: VUIDs stay 10-digit strings, errors name fields and never echo values, CSV and canonicalization round trips, `extra` loses nothing, DOB and EDR rejects, precinct matching, `parse_voter` against an independent oracle |
| `test_pure_properties.py` | Boundaries: Worker cron `should_dispatch` across the 2026-11-01 daylight-time switch and the end instant, scores and hashes, signed 64-bit row keys, `key_ranges` tiling, chunking and batching, age-tier birthdays |
| `test_stateful_roster.py` | `RuleBasedStateMachine` models: roster-row diff writes (idempotent reload, crash then rerun, marker written last), the repeat build's distinct-count invariant, and the backfill sequence |

Profiles are registered in `tests/conftest.py` and chosen by `HYPOTHESIS_PROFILE`, or `CI` in CI.

| Profile | Use |
|---|---|
| `tx-dev` | Default locally. 50 examples, no deadline. |
| `tx-ci` | Loaded when `CI` is set. Derandomized, 100 examples, no example database, prints the reproduction blob. |
| `tx-thorough` | `HYPOTHESIS_PROFILE=tx-thorough uv run pytest tests/unit/property`. 2000 examples, for a deep manual run. |

Conventions:

- A property failure that is a real defect in `src/` is first committed as `@pytest.mark.xfail(strict=True, reason=...)` with the shrunk synthetic example, then fixed in its own change. A fixed defect drops the marker.
- Pin every nasty case you find with `@example(...)` so it runs on every profile.
- Assert behaviour against an oracle written from the requirement, not a copy of the code under test.
- Keep a file under about 25 seconds on `tx-dev`. A failing stateful machine can take minutes to shrink, and the machines cap runs at 20 steps even under `tx-thorough`.

## Coverage targets

See project prompts and `docs/GUARDRAILS.md`. Unit tests should cover:

- Session establishment and `prime_election` call order
- Turnout HTML parsing and `extract_county_ids` (`townId` values)
- Roster CSV parsing (VUID as str, vote method normalization)
- `legacy_api` facade wiring
- Voterfile DuckDB VUID zero-padding (`lpad`)
