# cli-fetch-all — CLI fetch-all Command
# Version: 1.0.0
# Model: claude-sonnet-4-6
# Last Updated: 2026-05-24
# Maintainer: John Eakin

# Prompt 06 — CLI: fetch-all command + writer integration

## Goal
Add a `civix fetch-all` CLI command that fetches all EV dates for an election,
accumulates them into one per-election roster file, and writes the output using
`writer.accumulate_roster()` + `writer.write_roster_csv()`.

Also add the corresponding `legacy fetch-all` variant.

## Files to read first (required context)
- `src/texas_turnout_scraper/cli.py`
- `src/texas_turnout_scraper/civix.py`
- `src/texas_turnout_scraper/roster.py`
- `src/texas_turnout_scraper/writer.py`
- `src/texas_turnout_scraper/models.py`
- `docs/ARCHITECTURE_SPEC.md` (output path conventions)

## Files to modify
- `src/texas_turnout_scraper/cli.py`

## Files NOT to touch
- Any model or writer module
- Any test file
- Any fixture file

## Output file naming convention

Per-election roster CSV:
```
data/elections/{source}/{election_id}/roster_ev_{election_id}.csv
```

Examples:
- `data/elections/civix/53813/roster_ev_53813.csv`
- `data/elections/legacy/49664/roster_ev_49664.csv`

The file is CREATED or OVERWRITTEN each time `fetch-all` runs. All EV dates
are combined into one file. `REPORT_DATE` column distinguishes dates.

## `civix fetch-all` command spec

```bash
tx-turnout civix fetch-all <election-id> [OPTIONS]

Arguments:
  election-id    Civix election ID (numeric string, e.g. "53813")

Options:
  --output-dir   Base output directory [default: data/elections/civix]
  --pace         Seconds between requests [default: 1.0]
  --dry-run      Print what would be fetched without making requests
```

### Implementation steps
1. Call `CivixClient().list_elections()` to find the election by ID
2. If not found, exit with error: "Election {id} not found"
3. Print election name + EV dates to stdout
4. For each EV date:
   a. Call `CivixClient().fetch_ev_turnout()` to get county list
   b. For each county where `roster_available=True`:
      - Call `CivixClient().fetch_ev_roster_csv()` → `CountyRoster`
   c. Collect all county rosters for this date
5. After all dates: call `accumulate_roster(all_rosters)` → flagged records
6. Call `write_roster_csv(records, path)` → save to output path
7. Print summary: total records, unique VUIDs, duplicate count

### Progress output (stdout)
```
Election: 2026 REPUBLICAN PRIMARY ELECTION (53813)
EV dates: 3
[2026-02-17] Fetching 3 counties...  done (450 records)
[2026-02-18] Fetching 3 counties...  done (512 records)
[2026-02-27] Fetching 3 counties...  done (489 records)
Accumulating 1451 records...
  Unique VUIDs: 1448
  Duplicate flags: 3
Wrote: data/elections/civix/53813/roster_ev_53813.csv
```

## `legacy fetch-all` command spec

```bash
tx-turnout legacy fetch-all <election-id> [OPTIONS]

Arguments:
  election-id    Legacy SOS election ID (e.g. "49664")

Options:
  --output-dir   Base output directory [default: data/elections/legacy]
  --pace         Seconds between requests [default: 1.0]
  --dry-run      Print what would be fetched without making requests
  --county-ids   Comma-separated list of county IDs to fetch (default: all)
```

### Implementation steps
1. Establish `LegacySession` for the election
2. Call `get_ev_dates()` to discover available dates
3. For each EV date:
   a. Call `fetch_turnout()` to get county list + IDs
   b. Call `extract_county_ids()` to get county IDs
   c. Call `fetch_roster_strategy_a()` with county_ids + county_names map
      (derive county_names from turnout table)
   d. Collect all county rosters
4. After all dates: call `accumulate_roster(all_rosters)` → flagged records
5. Call `write_roster_csv(records, path)` → save to output path
6. Print summary

## Error handling

- If an individual county fails, log a warning and continue (don't abort the run)
- If an entire EV date returns 0 rosters, log a warning
- Non-zero exit code on complete failure (0 rosters across all dates)
- HTTP errors on individual counties: catch, log with county context (NOT VUID), continue

## Audit output (optional --audit flag)

If `--audit` flag is passed, also run `audit_from_records()` after accumulation
and write the report to:
```
data/elections/{source}/{election_id}/audit_ev_{election_id}.json
```

## Constraints
- All lazy imports inside command functions (don't import at module level)
- Never log or print `id_voter` or `voter_name` values
- Pacing must be ≥1.0 s — enforce even if user passes lower value via --pace
- `source_election_id` never coerced to int anywhere in CLI code

## Acceptance criteria
```bash
# Dry run (no network)
tx-turnout civix fetch-all 53813 --dry-run

# Real fetch (requires network)
tx-turnout civix fetch-all 53813
ls data/elections/civix/53813/
# roster_ev_53813.csv exists
```
