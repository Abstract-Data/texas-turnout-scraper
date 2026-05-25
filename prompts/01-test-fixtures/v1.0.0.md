# test-fixtures — Create Synthetic Test Fixtures
# Version: 1.0.0
# Model: claude-sonnet-4-6
# Last Updated: 2026-05-24
# Maintainer: John Eakin

# Prompt 01 — Create Test Fixtures

## Goal
Create synthesized fixture files used by the unit test suite.
All PII must be synthetic — no real voter names or VUIDs.

## Files to create

```
tests/fixtures/early_voting/
├── civix_election_index.json          # Decoded EVR_ELECTION response (2 elections)
├── civix_earlyvoting_53813.json       # Decoded EVR_EARLYVOTING for election 53813, one date
├── civix_roster_harris_sample.csv     # 10 synthetic voter rows — HARRIS county
├── civix_roster_travis_sample.csv     # 5 synthetic voter rows — TRAVIS county
├── civix_ed_turnout_53813.json        # Decoded EVR_ELECTIONDAYTURNOUT for election 53813
├── legacy_election_index.html         # SOS HTML with <select name="idElection"> dropdown
├── legacy_ev_dates_49664.html         # SOS HTML response from getElectionEVDates.do
├── legacy_ev_details_49664.html       # SOS HTML response from getEVDetails.do (county table)
└── legacy_voter_info_loving.csv       # Loving County — smallest TX county, 6 synthetic rows
```

## Fixture specs

### `civix_election_index.json`
This is the DECODED content of `{"upload": "<base64>"}` from `GET getFile?type=EVR_ELECTION`.

```json
{
  "date_updated": "05/24/2026",
  "elections": [
    {
      "id": 53813,
      "type": "EV",
      "election_date": "03/03/2026",
      "election_name": "2026 REPUBLICAN PRIMARY ELECTION",
      "certified": true,
      "early_voting_dates": [
        {"date": "02/17/2026", "date_turnout_id": 1},
        {"date": "02/18/2026", "date_turnout_id": 2},
        {"date": "02/27/2026", "date_turnout_id": 11}
      ],
      "counties": [
        {"county_id": 1, "name": "ANDERSON"},
        {"county_id": 101, "name": "HARRIS"},
        {"county_id": 227, "name": "TRAVIS"}
      ]
    },
    {
      "id": 56181,
      "type": "EV",
      "election_date": "05/03/2026",
      "election_name": "2026 SPECIAL ELECTION SENATE DISTRICT 4",
      "certified": false,
      "early_voting_dates": [
        {"date": "04/20/2026", "date_turnout_id": 1}
      ],
      "counties": [
        {"county_id": 12, "name": "BOWIE"},
        {"county_id": 37, "name": "CASS"}
      ]
    }
  ]
}
```

### `civix_earlyvoting_53813.json`
Decoded content of `GET getFile?type=EVR_EARLYVOTING&electionId=53813&electionDate=02/27/2026`.

```json
{
  "date_updated": "2026-05-12",
  "election_id": 53813,
  "election_type": "P",
  "early_voting_date": "2026-02-27",
  "early_voting_date_id": 925,
  "turnout_by_county": [
    {
      "name": "ANDERSON",
      "id": 1,
      "registered_voters": 30678,
      "in_person_votes_on_date": 707,
      "total_in_person_votes_for_election": 3963,
      "total_mail_votes_for_election": 0,
      "voter_details_report": "EarlyVotingTurnoutByDate/2026-02-27/EVR_EarlyVotingByDate"
    },
    {
      "name": "HARRIS",
      "id": 101,
      "registered_voters": 2400000,
      "in_person_votes_on_date": 14200,
      "total_in_person_votes_for_election": 88500,
      "total_mail_votes_for_election": 1200,
      "voter_details_report": "EarlyVotingTurnoutByDate/2026-02-27/EVR_EarlyVotingByDate"
    },
    {
      "name": "TRAVIS",
      "id": 227,
      "registered_voters": 900000,
      "in_person_votes_on_date": 0,
      "total_in_person_votes_for_election": 0,
      "total_mail_votes_for_election": 0,
      "voter_details_report": false
    }
  ]
}
```

### `civix_roster_harris_sample.csv`
Exactly 4 columns matching the SOS/Civix CSV schema. **All names and VUIDs are synthetic.**
VUIDs must be exactly 10 digits. Use clearly fake names (e.g. `DOE, JOHN`).

```csv
"VOTER_NAME","ID_VOTER","VOTING_METHOD","PRECINCT"
"DOE, JOHN A","0000000001","IN-PERSON","510"
"DOE, JANE B","0000000002","IN-PERSON","510"
"DOE, JAMES C","0000000003","MAIL-IN","512"
"DOE, JULIA D","0000000004","IN-PERSON","515"
"DOE, JEFFREY E","0000000005","IN-PERSON","515"
"DOE, JESSICA F","0000000006","MAIL-IN","518"
"DOE, JEROME G","0000000007","IN-PERSON","520"
"DOE, JENNY H","0000000008","IN-PERSON","520"
"DOE, JUSTIN I","0000000009","IN-PERSON","522"
"DOE, JOSEPHINE J","0000000010","MAIL-IN","525"
```

### `civix_roster_travis_sample.csv`
5 rows. Include one duplicate VUID (same VUID appears twice) so audit tests can detect it.

```csv
"VOTER_NAME","ID_VOTER","VOTING_METHOD","PRECINCT"
"SMITH, ALICE A","1000000001","IN-PERSON","100"
"SMITH, BOB B","1000000002","IN-PERSON","101"
"SMITH, CAROL C","1000000003","MAIL-IN","102"
"SMITH, DAVE D","1000000002","IN-PERSON","103"
"SMITH, EVE E","1000000004","IN-PERSON","104"
```
(Note: VUID `1000000002` appears twice — intentional for duplicate detection test.)

### `civix_ed_turnout_53813.json`
Decoded content of `GET getFile?type=EVR_ELECTIONDAYTURNOUT&electionId=53813&electionDate=03/03/2026`.
Same structure as earlyvoting, but `voter_details_report` is `true` (bool) for all counties
(certified election day data means ZIP endpoint, not CSV).

```json
{
  "date_updated": "2026-05-08",
  "election_id": 53813,
  "election_type": "P",
  "early_voting_date": null,
  "early_voting_date_id": null,
  "turnout_by_county": [
    {
      "name": "ANDERSON",
      "id": 1,
      "registered_voters": 30678,
      "in_person_votes_on_date": 2993,
      "total_in_person_votes_for_election": 6956,
      "total_mail_votes_for_election": 138,
      "voter_details_report": true
    },
    {
      "name": "HARRIS",
      "id": 101,
      "registered_voters": 2400000,
      "in_person_votes_on_date": 89000,
      "total_in_person_votes_for_election": 177500,
      "total_mail_votes_for_election": 2400,
      "voter_details_report": true
    }
  ]
}
```

### `legacy_election_index.html`
Minimal HTML page with the SOS election dropdown. Only the `<select name="idElection">` matters.

```html
<!DOCTYPE html>
<html>
<body>
<form>
<select name="idElection">
  <option value="">-- Select Election --</option>
  <option value="49664">2024 NOVEMBER 5TH GENERAL ELECTION</option>
  <option value="47832">2024 MARCH 5TH REPUBLICAN PRIMARY</option>
  <option value="47831">2024 MARCH 5TH DEMOCRATIC PRIMARY</option>
</select>
</form>
</body>
</html>
```

### `legacy_ev_dates_49664.html`
HTML response from `POST getElectionEVDates.do`. Contains a date dropdown.
EV date option values use the Struts format: `"YYYY-MM-DD 00:00:00.0"`.

```html
<!DOCTYPE html>
<html>
<body>
<select name="sEVDate" id="sEVDate">
  <option value="">-- Select Date --</option>
  <option value="2024-10-21 00:00:00.0">10/21/2024</option>
  <option value="2024-10-22 00:00:00.0">10/22/2024</option>
  <option value="2024-11-01 00:00:00.0">11/01/2024</option>
</select>
</body>
</html>
```

### `legacy_ev_details_49664.html`
HTML response from `POST getEVDetails.do`. Contains the county turnout table.
Use a minimal table with 3 counties. Include `onclick` attributes with county IDs.

```html
<!DOCTYPE html>
<html>
<body>
<table>
  <thead>
    <tr>
      <th>COUNTY</th>
      <th>REGISTERED VOTERS</th>
      <th>IN-PERSON ON DATE</th>
      <th>CUMULATIVE IN-PERSON</th>
      <th>CUMULATIVE MAIL</th>
      <th>CUMULATIVE TOTAL</th>
    </tr>
  </thead>
  <tbody>
    <tr onclick="downloadReport('149')">
      <td>LOVING</td><td>100</td><td>2</td><td>12</td><td>0</td><td>12</td>
    </tr>
    <tr onclick="downloadReport('101')">
      <td>HARRIS</td><td>2400000</td><td>14200</td><td>88500</td><td>1200</td><td>89700</td>
    </tr>
    <tr onclick="downloadReport('227')">
      <td>TRAVIS</td><td>900000</td><td>8500</td><td>52000</td><td>800</td><td>52800</td>
    </tr>
    <tr>
      <td>STATEWIDE</td><td>18657918</td><td>89000</td><td>550000</td><td>15000</td><td>565000</td>
    </tr>
  </tbody>
</table>
</body>
</html>
```

### `legacy_voter_info_loving.csv`
6 rows from Loving County (smallest Texas county by population).
**All synthetic.** Must have exactly these 4 columns.

```csv
"VOTER_NAME","ID_VOTER","VOTING_METHOD","PRECINCT"
"DOE, LOVING A","2000000001","IN-PERSON","1"
"DOE, LOVING B","2000000002","IN-PERSON","1"
"DOE, LOVING C","2000000003","MAIL-IN","1"
"DOE, LOVING D","2000000004","IN-PERSON","1"
"DOE, LOVING E","2000000005","IN-PERSON","1"
"DOE, LOVING F","2000000006","MAIL-IN","1"
```

## Constraints

- Never use real voter names or real VUIDs in fixtures
- VUIDs must be exactly 10 digits (pad with leading zeros if needed)
- Do not create test fixtures that look like real people (use "DOE, JOHN" style)
- JSON files should be pretty-printed (indent=2)
- CSV files should use double-quoted fields matching SOS format

## Acceptance criteria

```bash
ls tests/fixtures/early_voting/
# Should show all 9 files
python3 -c "import json; json.load(open('tests/fixtures/early_voting/civix_election_index.json'))"
# Should not raise
python3 -c "import csv; list(csv.DictReader(open('tests/fixtures/early_voting/civix_roster_harris_sample.csv')))"
# Should not raise
```
