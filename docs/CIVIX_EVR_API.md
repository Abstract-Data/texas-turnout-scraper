# Texas SOS Early Voting — Civix EVR API

**Base URL:** `https://goelect.txelections.civixapps.com`  
**UI Entry Point:** `/ivis-evr-ui/evr`  
**API Prefix:** `/api-ivis-system/api/v1/`  
**Observed:** 2026-05-24 | Build: v1.0.0-dev (May 05, 2026)

This document records the full HTTP API for the **new Civix-hosted** Texas SOS early-voting
report portal — the replacement for the legacy Java/Struts site documented in
[`EARLY_VOTING_ROSTER.md`](./EARLY_VOTING_ROSTER.md).

**Critical difference from legacy:** This API is **fully stateless**. No session, no cookies,
no JSESSIONID, no POST sequence. Every endpoint is a plain `GET` request. Any tool that can
make an HTTP request can consume this API.

---

## Response Envelope

**Every endpoint** — regardless of content type — returns JSON with a single `upload` key
containing base64-encoded payload:

```json
{ "upload": "<base64-encoded string>" }
```

The decoded content is either:
- A **JSON string** (for `getFile` endpoints) → parse with `json.loads(base64.b64decode(upload))`
- A **CSV string** (for `getFileByFormat?format=csv`) → decode then parse with `csv.DictReader`
- A **ZIP binary** (for `getFileByFormat?format=zip`) → decode with `base64.b64decode`, then `zipfile.ZipFile`

```python
import base64, json

resp = httpx.get(url)
envelope = resp.json()
decoded_bytes = base64.b64decode(envelope["upload"])

# For JSON endpoints:
data = json.loads(decoded_bytes)

# For CSV endpoints:
csv_text = decoded_bytes.decode("utf-8")

# For ZIP endpoints:
import io, zipfile
zf = zipfile.ZipFile(io.BytesIO(decoded_bytes))
```

---

## Endpoint Map

```
GET /api-ivis-system/api/v1/getFile?type=EVR_ELECTION
    → Election index (all elections + their EV dates + participating counties)

GET /api-ivis-system/api/v1/getFile?type=EVR_EARLYVOTING&electionId={id}&electionDate={date}
    → County EV turnout table (JSON) for one election + date

GET /api-ivis-system/api/v1/getFileByFormat?type=EVR_EARLYVOTING&electionId={id}&electionDate={date}&county={name}&countyId={id}&format=csv
    → Per-county EV voter roster (CSV, base64-wrapped)

GET /api-ivis-system/api/v1/getFile?type=EVR_STATEWIDE&electionId={id}&electionDate={date}
    → Statewide bulk roster (may 502 for large elections)

GET /api-ivis-system/api/v1/getFileByFormat?type=EVR_COUNTYPLACEINFO&electionId={id}&name={COUNTY_NAME|STATEWIDE_POLLING_PLACE_INFO}&format=csv
    → Polling place info CSV (per-county or statewide)

GET /api-ivis-system/api/v1/getFile?type=EVR_ELECTIONDAYTURNOUT&electionId={id}&electionDate={date}
    → County Election Day turnout table (JSON)

GET /api-ivis-system/api/v1/getFileByFormat?type=EVR_ELECTIONDAYTURNOUT&electionId={id}&electionDate={date}&county={name}&countyId={id}&format=zip
    → Per-county Election Day voter roster (ZIP containing CSV)
```

---

## Endpoint 1 — Election Index

```
GET /api-ivis-system/api/v1/getFile?type=EVR_ELECTION
```

No parameters. Returns all elections currently in the system.

### Decoded JSON Schema

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
        { "date": "02/17/2026", "date_turnout_id": 1 },
        { "date": "02/18/2026", "date_turnout_id": 2 },
        ...
      ],
      "counties": [
        { "county_id": 1, "name": "ANDERSON" },
        { "county_id": 2, "name": "ANDREWS" },
        ...
      ]
    }
  ]
}
```

| Field | Type | Notes |
|-------|------|-------|
| `id` | int | Civix election ID — use as `source_election_id` (store as string) |
| `type` | string | Always `"EV"` in observed data |
| `election_date` | string | `MM/DD/YYYY` format |
| `election_name` | string | Raw SOS text — use for `election_type` inference |
| `certified` | bool | `true` = final certified data; `false` = unofficial |
| `early_voting_dates[].date` | string | `MM/DD/YYYY` |
| `early_voting_dates[].date_turnout_id` | int | Sequential per-election date ID (1-based) |
| `counties[].county_id` | int | Civix county ID — needed for roster download |
| `counties[].name` | string | All-caps county name |

**Observed as of 2026-05-24:** 10 elections (mix of certified and uncertified, 2025–2026).

### Election Type Inference

Infer from `election_name` (consistent with legacy ingest):

| Name contains | `election_type` |
|--------------|-----------------|
| `PRIMARY RUNOFF` | `primary_runoff` |
| `PRIMARY` | `primary` |
| `GENERAL` | `general` |
| `SPECIAL RUNOFF` | `special` |
| `SPECIAL` | `special` |
| `CONSTITUTIONAL` | `constitutional_amendment` |
| `LOCAL` | `local` |
| else | `unknown` |

---

## Endpoint 2 — EV Turnout by Date (County Summary)

```
GET /api-ivis-system/api/v1/getFile?type=EVR_EARLYVOTING&electionId={id}&electionDate={date}
```

| Parameter | Format | Example |
|-----------|--------|---------|
| `electionId` | int | `53813` |
| `electionDate` | `MM/DD/YYYY` | `02/27/2026` |

### Decoded JSON Schema

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
    }
  ]
}
```

| Field | Type | Notes |
|-------|------|-------|
| `date_updated` | string | ISO date `YYYY-MM-DD` — when data was last updated |
| `election_id` | int | Civix election ID |
| `election_type` | string | `"P"` (primary), `"G"` (general), `"S"` (special), etc. |
| `early_voting_date` | string | ISO date `YYYY-MM-DD` |
| `early_voting_date_id` | int | Internal sequential ID across all elections |
| `turnout_by_county[].name` | string | County name (all-caps) |
| `turnout_by_county[].id` | int | Civix county ID — use as `countyId` in roster requests |
| `turnout_by_county[].registered_voters` | int | Total registered voters in county |
| `turnout_by_county[].in_person_votes_on_date` | int | In-person voters **on this date only** |
| `turnout_by_county[].total_in_person_votes_for_election` | int | Cumulative in-person through this date |
| `turnout_by_county[].total_mail_votes_for_election` | int | Cumulative mail-in through this date |
| `turnout_by_county[].voter_details_report` | string or bool | Path string when roster available; `true` when certified; `false` when unavailable |

> **Note:** `voter_details_report` changes type across report states. Always check with
> `isinstance(val, str)` before using as a path. When `true` (bool), use the ZIP download
> endpoint. When a path string, use the CSV endpoint.

---

## Endpoint 3 — EV Per-County Voter Roster (CSV)

```
GET /api-ivis-system/api/v1/getFileByFormat?type=EVR_EARLYVOTING
    &electionId={id}&electionDate={date}
    &county={COUNTY_NAME}&countyId={county_id}&format=csv
```

| Parameter | Format | Example |
|-----------|--------|---------|
| `electionId` | int | `53813` |
| `electionDate` | `MM/DD/YYYY` | `02/27/2026` |
| `county` | string | `HARRIS` (all-caps) |
| `countyId` | int | `101` (from election index or turnout response) |
| `format` | string | `csv` |

### CSV Schema (identical to legacy SOS portal)

```csv
"VOTER_NAME","ID_VOTER","VOTING_METHOD","PRECINCT"
"AARABI, SIAVASH","2196037195","IN-PERSON","510"
"AARABI, SABA","2191022420","IN-PERSON","510"
```

| Column | Type | Notes |
|--------|------|-------|
| `VOTER_NAME` | string | Full name, last-first format (**PII**) |
| `ID_VOTER` | 10-digit string | Texas VUID (**PII**) — keep as string, never int |
| `VOTING_METHOD` | enum | `IN-PERSON` or `MAIL-IN` |
| `PRECINCT` | string | Precinct number |

Response is base64-wrapped like all other endpoints. Decode to get raw CSV text.

**Empty roster:** For counties with zero voters on that EV date, Civix may return HTTP
200 with an **empty response body** (not `{"upload": "..."}`). The scraper treats that as
an empty roster (0 records). Turnout may still show `voter_details_report` as available.

---

## Endpoint 4 — EV Statewide Bulk

```
GET /api-ivis-system/api/v1/getFile?type=EVR_STATEWIDE&electionId={id}&electionDate={date}
```

Returns the entire state roster in one response. **Returns HTTP 502 for large certified
elections** (e.g. 2026 Republican Primary with 254 counties). May succeed for small
special elections. Test before relying on this endpoint for statewide pulls.

**Strategy recommendation:** Use per-county loop (Endpoint 3) for reliability.

---

## Endpoint 5 — Polling Place Info (CSV)

```
GET /api-ivis-system/api/v1/getFileByFormat?type=EVR_COUNTYPLACEINFO
    &electionId={id}&name={name}&format=csv
```

| `name` value | Returns |
|--------------|---------|
| `STATEWIDE_POLLING_PLACE_INFO` | All polling places statewide |
| `{COUNTY_NAME}` (e.g. `HARRIS`) | Polling places for one county |

### CSV Schema

```csv
"ELECTION ID","ELECTION NAME","COUNTY NAME","POLL PLACE ID","POLL PLACE NAME","ADDRESS","POLLING PLACE TYPE","DATE AND TIMINGS","PRECINCTS"
"53813","2026 REPUBLICAN PRIMARY ELECTION","VICTORIA","19800","00 PATTIE DODSON PUBLIC HEALTH CENTER","2805 N. NAVARRO VICTORIA TX 77901","EV","02/17/2026-02/27/2026 8:00 AM-5:00 PM","1; 10; 11; 12; ..."
```

| Column | Type | Notes |
|--------|------|-------|
| `ELECTION ID` | string | Civix election ID |
| `ELECTION NAME` | string | Full election name |
| `COUNTY NAME` | string | All-caps county name |
| `POLL PLACE ID` | string | Unique poll place identifier |
| `POLL PLACE NAME` | string | Facility name |
| `ADDRESS` | string | Full street address |
| `POLLING PLACE TYPE` | enum | `EV` (early voting) or `ED` (election day) |
| `DATE AND TIMINGS` | string | Semicolon-separated date ranges with hours |
| `PRECINCTS` | string | Semicolon-separated precinct numbers served |

---

## Endpoint 6 — Election Day Turnout (County Summary)

```
GET /api-ivis-system/api/v1/getFile?type=EVR_ELECTIONDAYTURNOUT&electionId={id}&electionDate={date}
```

| Parameter | Format | Example |
|-----------|--------|---------|
| `electionId` | int | `53813` |
| `electionDate` | `MM/DD/YYYY` | `03/03/2026` (the actual election date) |

### Decoded JSON Schema

Identical structure to EVR_EARLYVOTING (Endpoint 2):

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
    }
  ]
}
```

`voter_details_report` is `true` (bool) for certified election day data — use ZIP endpoint.

---

## Endpoint 7 — Election Day Per-County Voter Roster (ZIP)

```
GET /api-ivis-system/api/v1/getFileByFormat?type=EVR_ELECTIONDAYTURNOUT
    &electionId={id}&electionDate={date}
    &county={COUNTY_NAME}&countyId={county_id}&format=zip
```

Returns a base64-wrapped ZIP. Decode with `base64.b64decode`, open with `zipfile.ZipFile`.
Contents: one or more CSV files with the voter roster (same schema as Endpoint 3).

---

## Request Flow

```
1. GET /getFile?type=EVR_ELECTION
   → Discover all elections + their EV dates + county lists

2. For each election × EV date:
   GET /getFile?type=EVR_EARLYVOTING&electionId={id}&electionDate={date}
   → County turnout table (254 rows for statewide election)

3. For each county in the turnout table (if roster needed):
   GET /getFileByFormat?type=EVR_EARLYVOTING&...&county={name}&countyId={id}&format=csv
   → Per-county voter roster CSV

4. After election day:
   GET /getFile?type=EVR_ELECTIONDAYTURNOUT&electionId={id}&electionDate={election_date}
   → Election day county summary

5. For election day rosters:
   GET /getFileByFormat?type=EVR_ELECTIONDAYTURNOUT&...&format=zip
   → Per-county ZIP containing roster CSV
```

---

## Ingest Strategies

### Strategy A — Per-County Loop (Recommended)

```
For each election:
  Fetch election index → get EV dates + county list
  For each EV date:
    Fetch county turnout table
    For each county:
      Fetch per-county roster CSV
      Parse + store VoterRecord rows
  Fetch election day turnout table
  For each county:
    Fetch election day roster ZIP
```

- ~255 requests per election date (1 summary + 254 counties)
- Pace at ≥1.0 s between requests
- Preserves `VOTING_METHOD` (IN-PERSON / MAIL-IN) split
- Works reliably for all election sizes

### Strategy B — Statewide Bulk (Unreliable for Large Elections)

```
GET /getFile?type=EVR_STATEWIDE&electionId={id}&electionDate={date}
```

- Single request, but **502s for large elections** (254-county statewide)
- Suitable only for small special elections (1–5 counties)
- Do not use as primary strategy until 502 behavior is better characterized

---

## Key Differences from Legacy SOS Portal

| Aspect | Legacy (`earlyvoting.texas-election.com`) | Civix (`goelect.txelections.civixapps.com`) |
|--------|------------------------------------------|---------------------------------------------|
| Auth | Stateful — JSESSIONID cookie required | **Stateless — no session, no cookies** |
| Method | POST form (`application/x-www-form-urlencoded`) | **GET with query params** |
| Response | Raw HTML (screen-scrape) or raw CSV/ZIP | **JSON envelope `{"upload": "<base64>"}`** |
| Election index | Parse `<select name="idElection">` from HTML | **`GET getFile?type=EVR_ELECTION`** |
| County ID discovery | Scrape `onclick="downloadReport('{townId}')"` | **In election index response as `county_id`** |
| EV date format | `YYYY-MM-DD HH:MM:SS.0` in POST body | `MM/DD/YYYY` in query param |
| Turnout response | HTML table (screen-scrape) | **Structured JSON** |
| Roster CSV | Raw CSV (no wrapper) | **Base64-wrapped** |
| Election Day roster | Raw CSV | **Base64-wrapped ZIP** |
| Polling place | Screen-scraped from separate page | **Dedicated CSV endpoint** |
| `certified` field | Not exposed | **Boolean in election index** |
| Pacing required | Yes (≥1.0 s) | Yes (≥1.0 s, be conservative) |

---

## Pydantic Models

```python
class CivixElectionDate(BaseModel):
    date: date                    # parsed from MM/DD/YYYY
    date_turnout_id: int

class CivixCountyRef(BaseModel):
    county_id: int
    name: str

class CivixElection(BaseModel):
    source_election_id: str       # str(id) — always string
    id: int                       # raw int from API
    type: str                     # "EV"
    election_date: date           # parsed from MM/DD/YYYY
    election_name: str
    election_type: ElectionType   # inferred
    certified: bool
    early_voting_dates: List[CivixElectionDate]
    counties: List[CivixCountyRef]

class CivixCountyTurnout(BaseModel):
    election_id: str              # str(election_id)
    report_date: date             # early_voting_date
    county: str
    county_id: int
    registered_voters: int
    in_person_votes_on_date: int
    total_in_person_votes: int    # cumulative
    total_mail_votes: int         # cumulative
    roster_available: bool        # derived from voter_details_report field
```

---

## Observed Sample Data (2026-05-24)

| Election | ID | Counties | EV Dates | Certified |
|----------|----|----------|----------|-----------|
| 2026 DEMOCRATIC PRIMARY RUNOFF | TBD | 254 | TBD | false |
| 2026 REPUBLICAN PRIMARY RUNOFF | TBD | 254 | TBD | false |
| 2026 SPECIAL ELECTION SENATE DISTRICT 4 | 56181 | 5 | 9 | false |
| 2026 REPUBLICAN PRIMARY ELECTION | 53813 | 254 | 11 | true |
| 2026 DEMOCRATIC PRIMARY ELECTION | TBD | 254 | TBD | TBD |
| 2026 SPECIAL RUNOFF ELECTION CD-18 | 54612 | 1 (HARRIS) | 10 | false |
| 2026 SPECIAL RUNOFF ELECTION SD-9 | TBD | TBD | TBD | false |
| 2025 SPECIAL ELECTION SD-9 | TBD | TBD | TBD | TBD |
| 2025 NOVEMBER 4TH CONSTITUTIONAL AMENDMENT | TBD | 254 | TBD | TBD |
| 2025 SPECIAL ELECTION CD-18 | TBD | TBD | TBD | TBD |

Republican Primary 2026 (id=53813, certified): 2,092,983 election day in-person voters,
41,425 mail — 18,657,918 registered voters statewide.

---

## Open Questions

1. **EVR_STATEWIDE 502 threshold** — at what county count does the statewide endpoint fail?
   Test with the 5-county SD-4 special election before certifying Strategy B.
2. **Election date format in query params** — the API accepted `MM/DD/YYYY` in observed calls.
   Verify it also accepts `YYYY-MM-DD` for consistency with the decoded response fields.
3. **`voter_details_report` type rules** — confirm: string = CSV available, `true` = ZIP only,
   `false` = no roster yet. Test on an uncertified election in-progress.
4. **Historical elections** — does the Civix portal carry pre-2025 elections? The observed
   index had 10 elections (2025–2026). The legacy portal covers pre-2025.
5. **Rate limiting** — no observed rate limiting in testing, but pace at ≥1.0 s to be safe.

---

## Provenance

- Observed via browser network inspection at `goelect.txelections.civixapps.com`
- Angular Material (mat-mdc) UI, build v1.0.0-dev May 05 2026
- All endpoints return `200 OK` with `application/json` content type
- `EVR_STATEWIDE` returned `502` for election id=53813 (254 counties, certified)
- Observation date: 2026-05-24
