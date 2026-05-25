# voterfile-match — Voterfile Match Unit Tests
# Version: 1.0.0
# Model: claude-sonnet-4-6
# Last Updated: 2026-05-24
# Maintainer: John Eakin

# Prompt 09 — Voterfile Match: Unit Tests + Integration Verification

## Goal
Write unit tests for `voterfile.py` and verify the end-to-end CLI walkthrough
works correctly against the real Texas statewide voterfile.

## Files to read first (required context)
- `src/texas_turnout_scraper/voterfile.py`
- `src/texas_turnout_scraper/models.py` (ColumnMapping, EnrichedVoterRecord, VoterfileMatchReport)
- `src/texas_turnout_scraper/cli.py` (voterfile_app commands)
- `tests/fixtures/early_voting/civix_roster_harris_sample.csv`

## Files to create
- `tests/unit/test_voterfile.py`
- `tests/fixtures/voterfiles/sample_voterfile.csv`  (synthetic, no real PII)

## Sample voterfile fixture

Create `tests/fixtures/voterfiles/sample_voterfile.csv` with exactly this format
(matching the Texas SOS state voterfile schema used by `texasmay2026.csv`):

```csv
"COUNTY","PCT","CDPLANC2333","SD2022","HD2022","VUID","LNAME","FNAME","DOB","STATUS","SEX","HISPANIC"
"HARRIS","0510","7","6","145","0000000001","DOE","JOHN","19850315","V","M","N"
"HARRIS","0510","7","6","145","0000000002","DOE","JANE","19920701","V","F","Y"
"HARRIS","0512","7","6","145","0000000003","DOE","JAMES","20001201","V","M","N"
"HARRIS","0515","7","6","147","0000000004","DOE","JULIA","19781022","V","F","N"
"HARRIS","0515","7","6","147","0000000005","DOE","JEFFREY","19550614","V","M","N"
"HARRIS","0518","7","6","147","0000000006","DOE","JESSICA","19631130","V","F","Y"
"HARRIS","0520","7","6","148","0000000007","DOE","JEROME","19420801","V","M","N"
"HARRIS","0520","7","6","148","0000000008","DOE","JENNY","19391215","V","F","N"
"HARRIS","0522","7","6","148","0000000009","DOE","JUSTIN","19901010","V","M","N"
"TRAVIS","0100","35","14","46","0000000010","DOE","JOSEPHINE","19750820","V","F","Y"
```

VUIDs match the `civix_roster_harris_sample.csv` fixture (0000000001 through 0000000010).
All names/VUIDs are synthetic — not real people.

## Test structure

### Column detection tests

#### `test_detect_columns_exact_match`
```python
def test_detect_columns_exact_match(tmp_path):
    # Create a CSV with exact column names: VUID, COUNTY, PCT, DOB, SEX, STATUS, HISPANIC
    # Assert mapping.vuid == "VUID"
    # Assert mapping.county == "COUNTY"
    # Assert mapping.precinct == "PCT"
    # Assert confidence["vuid"] == "✓ Exact"
```

#### `test_detect_columns_prefix_pattern`
```python
def test_detect_columns_prefix_pattern(tmp_path):
    # Create a CSV with Texas-style names: CDPLANC2333, HD2022, SD2022
    # Assert mapping.cd == "CDPLANC2333"
    # Assert mapping.hd == "HD2022"
    # Assert mapping.sd == "SD2022"
    # Assert confidence["cd"] == "~ Prefix"
```

#### `test_detect_columns_alternate_names`
```python
def test_detect_columns_alternate_names(tmp_path):
    # Create a CSV with VAN-style names: VANID, LAST, FIRST, BIRTH_DATE
    # (or L2-style: LALVOTERID, LNAME, FNAME)
    # Assert vuid and name fields are detected
```

#### `test_detect_columns_undetected_field`
```python
def test_detect_columns_undetected_field(tmp_path):
    # CSV with no CD/HD/SD columns
    # Assert mapping.cd is None
    # Assert confidence["cd"] == "✗ Not detected"
```

### Age bracket tests

#### `test_age_bracket_18_24`
```python
def test_age_bracket_18_24():
    # DOB that makes voter 22 today
    # Assert age_bracket(dob) == "18-24"
```

#### `test_age_bracket_yyyymmdd_format`
```python
def test_age_bracket_yyyymmdd_format():
    # DOB in YYYYMMDD format (Texas state format)
    # Assert parses correctly and returns bracket
```

#### `test_age_bracket_all_ranges`
```python
@pytest.mark.parametrize("age,expected", [
    (18, "18-24"), (24, "18-24"),
    (25, "25-34"), (34, "25-34"),
    (35, "35-44"), (44, "35-44"),
    (45, "45-54"), (54, "45-54"),
    (55, "55-64"), (64, "55-64"),
    (65, "65-74"), (74, "65-74"),
    (75, "75+"), (90, "75+"),
])
def test_age_bracket_all_ranges(age, expected):
    # Build DOB from (reference_date - age years)
    # Assert age_bracket(dob, reference_date) == expected
```

#### `test_age_bracket_blank_returns_none`
```python
def test_age_bracket_blank_returns_none():
    assert age_bracket("") is None
    assert age_bracket("NULL") is None
    assert age_bracket(None) is None
```

### Match logic tests

#### `test_match_voterfile_to_roster_basic`
```python
def test_match_voterfile_to_roster_basic(tmp_path):
    # Use sample_voterfile.csv fixture + 3 VoterRecords matching VUIDs 1/2/3
    # Assert all 3 records have in_voterfile=True
    # Assert cd, hd, sd populated correctly
    # Assert age_bracket set (VUID 1: born 1985 → "35-44" or similar depending on ref date)
```

#### `test_match_voterfile_unmatched_records`
```python
def test_match_voterfile_unmatched_records(tmp_path):
    # Use sample_voterfile.csv + 1 VoterRecord with a VUID NOT in fixture
    # Assert in_voterfile=False, cd=None, hd=None, age_bracket=None
    # Assert report has an "unmatched_voters" finding
```

#### `test_match_voterfile_vuid_zero_padding`
```python
def test_match_voterfile_vuid_zero_padding(tmp_path):
    # Voterfile has VUID "1000001" (no leading zeros)
    # Roster has id_voter "0001000001" (10-digit padded)
    # Assert they still match after zero-padding normalisation
```

#### `test_match_report_counts`
```python
def test_match_report_counts(tmp_path):
    # 5 roster records, 3 in voterfile, 2 not
    # Assert report.matched_count == 3
    # Assert report.unmatched_count == 2
    # Assert report.match_rate == 0.6
```

#### `test_match_report_age_breakdown`
```python
def test_match_report_age_breakdown(tmp_path):
    # Use fixture with known DOBs → predictable age brackets
    # Assert report.by_age_bracket has expected keys
```

#### `test_match_no_pii_in_findings`
```python
def test_match_no_pii_in_findings(tmp_path):
    # Run a match with an unmatched record
    # Assert no VUID appears in any finding.detail string
    # Assert no voter name appears in any finding.detail string
```

### CSV round-trip

#### `test_write_enriched_csv_roundtrip`
```python
def test_write_enriched_csv_roundtrip(tmp_path):
    # Build 2 EnrichedVoterRecord objects
    # write_enriched_csv(records, path)
    # Read path back with csv.DictReader
    # Assert ID_VOTER preserved as string with leading zeros
    # Assert IN_VOTERFILE is "true"/"false" (lowercase)
    # Assert AGE_BRACKET value present
```

### Mapping persistence

#### `test_save_and_load_mapping`
```python
def test_save_and_load_mapping(tmp_path):
    mapping = ColumnMapping(vuid="VUID", cd="CDPLANC2333", hd="HD2022", sd="SD2022")
    path = tmp_path / "test.mapping.json"
    save_mapping(mapping, path)
    loaded = load_mapping(path)
    assert loaded.vuid == "VUID"
    assert loaded.cd == "CDPLANC2333"
```

## County mismatch audit finding

The match should detect and flag records where roster county ≠ voterfile county:

```python
def test_county_mismatch_finding(tmp_path):
    # Roster record: county="HARRIS"
    # Voterfile row for same VUID: county="TRAVIS" (different)
    # Assert report has finding_type=="county_mismatch"
```

## Voterfile column structure reference

The Texas SOS statewide voterfile (`texasmay2026.csv`) has these key columns:

| Column | Maps to | Notes |
|--------|---------|-------|
| `VUID` | vuid | 10-digit Texas VUID |
| `CDPLANC2333` | cd | Congressional District (2022 Planco map) |
| `HD2022` | hd | State House District |
| `SD2022` | sd | State Senate District |
| `COUNTY` | county | County name (all-caps) |
| `PCT` | precinct | Precinct number (zero-padded, e.g. "0510") |
| `LNAME` | last_name | Last name |
| `FNAME` | first_name | First name |
| `DOB` | dob | Date of birth in YYYYMMDD format |
| `SEX` | sex | M/F |
| `HISPANIC` | hispanic | Y/N |
| `STATUS` | status | V=active, S=suspense |

Vote history columns (`GEN24`, `PRI26`, etc.) use these codes:
- `VE` = voted early in-person
- `VA` = voted absentee (mail)
- `V` = voted on election day
- `RE` / `DE` = voted early Republican/Democratic primary
- `RA` / `DA` = voted absentee Republican/Democratic primary

## Constraints
- Never assert on actual voter_name values from fixtures in error messages
- Always test that isinstance(r.id_voter, str) for all records
- All fixture VUIDs must be clearly synthetic (0000000001 pattern)
- DuckDB must not be called in unit tests — mock the file read or use the fixture file directly

## Acceptance criteria
```bash
pytest tests/unit/test_voterfile.py -v
# All tests pass

# Smoke test against real voterfile (requires network + file access):
tx-turnout voterfile detect-columns /path/to/texasmay2026.csv
# All 12 fields detected
```

## Real-world CLI usage

```bash
# Step 1: See what columns will be detected
tx-turnout voterfile detect-columns texasmay2026.csv

# Step 2: Run a match (interactive walkthrough)
tx-turnout voterfile match roster_ev_53813.csv texasmay2026.csv

# Step 3: Non-interactive (accept auto-detected mapping)
tx-turnout voterfile match roster_ev_53813.csv texasmay2026.csv --no-interactive

# Step 4: Use a previously saved mapping
tx-turnout voterfile match roster_ev_53813.csv texasmay2026.csv \
    --mapping-file texasmay2026.mapping.json --no-interactive
```

The sidecar mapping file is saved as `texasmay2026.mapping.json` next to the voterfile
on the first run.  Subsequent runs load it automatically.
