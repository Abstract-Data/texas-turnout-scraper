# cleanup-and-verification — Cleanup and Final Verification
# Version: 1.0.0
# Model: claude-sonnet-4-6
# Last Updated: 2026-05-24
# Maintainer: John Eakin

# Prompt 08 — Cleanup and Final Verification

## Goal
Delete dead code, fix any remaining lint issues, run the full test suite,
and produce a final verification report confirming the refactor is complete.

## Files to read first (required context)
- `src/texas_turnout_scraper/` (all files)
- `tests/unit/` (all test files)
- `pyproject.toml`
- `HANDOFF.md`

## Steps

### 1. Delete dead code

**Delete `results_scraper.py`** — this is the old Selenium-based scraper.
It contains banned imports (`selenium`, `election_utils`) and is no longer used.

```bash
rm src/texas_turnout_scraper/results_scraper.py
```

Verify it is not imported anywhere:
```bash
grep -r "results_scraper" src/ tests/
# Should return nothing
```

### 2. Verify banned imports are gone

```bash
grep -r "from selenium" src/
grep -r "import selenium" src/
grep -r "from election_utils" src/
grep -r "import election_utils" src/
# All should return nothing
```

### 3. Verify ID coercion guardrails

```bash
# No int() coercion of election IDs or VUIDs anywhere
grep -n "int(.*election_id" src/texas_turnout_scraper/*.py
grep -n "int(.*id_voter" src/texas_turnout_scraper/*.py
grep -n "int(.*source_election_id" src/texas_turnout_scraper/*.py
# All should return nothing
```

### 4. Verify PII guardrails

```bash
# VOTER_NAME / voter_name must not appear in any logger.* call
grep -n "logger.*voter_name\|logger.*VOTER_NAME" src/texas_turnout_scraper/*.py
# Should return nothing

# id_voter must not appear in any logger.* call
grep -n "logger.*id_voter\|logger.*ID_VOTER" src/texas_turnout_scraper/*.py
# Should return nothing (only comments/docstrings, not logger calls)
```

### 5. Run ruff

```bash
ruff check . --fix
ruff format .
# Both should exit 0 with no errors
```

### 6. Run unit tests

```bash
pytest tests/unit/ -v
# All tests should pass
```

Check that these test files all pass:
- `tests/unit/test_enums.py`
- `tests/unit/test_models.py`
- `tests/unit/test_audit.py`
- `tests/unit/test_writer.py`
- `tests/unit/test_civix.py`
- `tests/unit/test_legacy.py`

### 7. Verify all source modules compile

```bash
python -c "
import texas_turnout_scraper
from texas_turnout_scraper import (
    CivixClient, accumulate_roster, write_roster_csv,
    read_roster_csv, audit_from_records,
    VoterRecord, CountyRoster, AuditReport,
    ElectionType, VoteMethod,
)
print('All imports OK')
"
```

### 8. Verify CLI entry point

```bash
tx-turnout --help
tx-turnout civix --help
tx-turnout legacy --help
tx-turnout audit --help
# All should print help without errors
```

### 9. Verify test fixtures exist

```bash
ls tests/fixtures/early_voting/
# Should show all 9 files:
# civix_election_index.json
# civix_earlyvoting_53813.json
# civix_roster_harris_sample.csv
# civix_roster_travis_sample.csv
# civix_ed_turnout_53813.json
# legacy_election_index.html
# legacy_ev_dates_49664.html
# legacy_ev_details_49664.html
# legacy_voter_info_loving.csv

# Validate JSON fixtures
python -c "import json; json.load(open('tests/fixtures/early_voting/civix_election_index.json'))"
python -c "import json; json.load(open('tests/fixtures/early_voting/civix_earlyvoting_53813.json'))"
python -c "import json; json.load(open('tests/fixtures/early_voting/civix_ed_turnout_53813.json'))"

# Validate CSV fixtures
python -c "import csv; list(csv.DictReader(open('tests/fixtures/early_voting/civix_roster_harris_sample.csv')))"
python -c "import csv; list(csv.DictReader(open('tests/fixtures/early_voting/legacy_voter_info_loving.csv')))"
```

### 10. Write final HANDOFF.md

Update `HANDOFF.md` at repo root with:

```markdown
# HANDOFF — Refactor Complete

## Completed this session
- All 8 prompts written to /prompts/
- roster.py updated: VoterRecord now receives county/election_id/report_date/voter_name
- __init__.py updated: exports accumulate_roster, write_roster_csv, read_roster_csv,
  roster_csv_to_text, audit_from_records
- test_models.py updated: VoterRecord tests use all required fields
- test_audit.py updated: _voter() helper passes county/election_id/report_date
- test_writer.py created: full coverage of accumulate_roster + CSV I/O
- results_scraper.py deleted

## State at handoff
- All unit tests pass (pytest tests/unit/ -v)
- ruff clean
- All 5 duplicate types implemented and tested
- One-file-per-election output pattern in place

## Next suggested actions
1. Run `pytest tests/unit/ -v` — confirm all pass locally
2. Execute prompts 02 (test_civix.py) and 03 (test_legacy.py)
3. Execute prompt 05 (integration tests) — requires network
4. Execute prompt 06 (CLI fetch-all command)
5. Execute prompt 07 (data-refresh GitHub Actions workflow)
6. Open PR: feature/httpx-refactor → main
```

## Constraints
- Do not modify any source module in this prompt — verification only
- If ruff or pytest fail, report the errors and stop (do not auto-fix)
- The only file deletions are `results_scraper.py` (dead code)

## Acceptance criteria
```bash
# The following command produces 0 failures
pytest tests/unit/ -v && ruff check . && echo "ALL CLEAN"
```
