# unit-tests-writer — Unit Tests: writer.py
# Version: 1.0.0
# Model: claude-sonnet-4-6
# Last Updated: 2026-05-24
# Maintainer: John Eakin

# Prompt 04 — Unit Tests: writer.py (accumulate_roster + CSV I/O)

## Goal
Verify that `tests/unit/test_writer.py` already exists and all tests pass.
If it doesn't exist, create it. If it exists but is incomplete, fill in any gaps.

## Files to read first (required context)
- `src/texas_turnout_scraper/writer.py`
- `src/texas_turnout_scraper/models.py`
- `src/texas_turnout_scraper/enums.py`
- `tests/unit/test_writer.py` (may already exist)

## Files to create or verify
- `tests/unit/test_writer.py`

## Files NOT to touch
- Any source module under `src/`

## Test coverage required

### `accumulate_roster` — core duplicate detection

All five duplicate conditions must have dedicated tests:

| Flag | Condition to reproduce |
|------|------------------------|
| `multiple_dates` | Same VUID in two rosters with different `report_date` |
| `conflicting_method` | Same VUID with `IN_PERSON` and `MAIL_IN` in same roster |
| `multiple_counties` | Same VUID in rosters for two different counties |
| `name_mismatch` | Same VUID, two records with different `voter_name` |
| `precinct_mismatch` | Same VUID, two records with different `precinct` |

Additional accumulate tests:
- Empty roster list → returns `[]`
- No duplicates → all records have `duplicate_flag=False`, `duplicate_type=""`, `also_found_on=""`
- Multiple flags on one VUID (e.g., `multiple_dates` AND `conflicting_method`)
- `also_found_on` contains all OTHER appearances, never the current row's own token
- Input records are NOT mutated (check original roster records unchanged)

### CSV round-trip

- `write_roster_csv` → `read_roster_csv` round-trip preserves all fields
- Leading zeros in `id_voter` are preserved through CSV round-trip
- `VOTER_NAME` is written to and read from CSV (it's a SOS public record field)
- `duplicate_flag` serializes as `"true"` / `"false"` (lowercase) in CSV

### `roster_csv_to_text`

- Output contains CSV header with all 10 columns
- `DUPLICATE_FLAG`, `DUPLICATE_TYPE`, `ALSO_FOUND_ON` columns present

### `audit_from_records`

- Counts match: `total_records`, `unique_vuids`, `duplicate_vuid_count`, `cross_method_duplicate_count`
- Findings list non-empty for duplicated VUIDs
- **PII guard**: no VUID values appear in any `finding.detail` string
- `election_id` and `report_date` from parameters override record-level values

## Key test patterns

```python
from datetime import date
import tempfile
from pathlib import Path
from texas_turnout_scraper.enums import VoteMethod
from texas_turnout_scraper.models import CountyRoster, VoterRecord
from texas_turnout_scraper.writer import (
    accumulate_roster, write_roster_csv, read_roster_csv,
    roster_csv_to_text, audit_from_records,
)

def _rec(vuid, method=VoteMethod.IN_PERSON, county="HARRIS",
         report_date=date(2026,2,17), precinct="100", voter_name=""):
    return VoterRecord(
        id_voter=vuid, voting_method=method, precinct=precinct,
        county=county, election_id="53813", report_date=report_date,
        voter_name=voter_name,
    )

def _roster(county, records, report_date=date(2026,2,17)):
    return CountyRoster(
        county=county, county_id=101, election_id="53813",
        report_date=report_date, source="civix", records=records,
    )
```

## Constraints
- All tests are pure Python (no HTTP mocking needed)
- Never print, log, or assert on voter_name content in failure messages
  (use `assert result[0].voter_name != ""` not `assert result[0].voter_name == "DOE, JOHN A"`)
  EXCEPTION: fixture-based tests that need to confirm the name is stored may check against
  known synthetic fixture values (e.g. "DOE, JOHN A") since these are not real PII

## Acceptance criteria
```bash
pytest tests/unit/test_writer.py -v
# All tests pass
```
