# WS-2B — VoterRecord.from_csv_row
# Version: 1.1.0 (workstream)
# Parent: [v1.1.0.md](../v1.1.0.md) · Manifest: [parallel-manifest.md](../parallel-manifest.md)

## Agent contract

| Field | Value |
|-------|-------|
| Branch | `feature/review-remediation/ws-2b-csv-row` |
| Base | `feature/review-remediation` (after WS-2A merged) |
| Model | claude-sonnet-4-6 |
| Wave | 2 |
| Exec | sequential[after: 2A] on `roster.py` |

### File lock (MAY edit)

- `src/texas_turnout_scraper/models.py` (`VoterRecord.from_csv_row`)
- `src/texas_turnout_scraper/civix.py` (call sites)
- `src/texas_turnout_scraper/roster.py` (`_parse_county_csv` only)
- `AGENTS.md` Learned Workspace Facts (`.zfill(10)` note)

### Forbidden (MUST NOT touch)

- `session.py` public API (WS-2A)
- `cli.py`

### Close issues

- RF-DRY-002

### On conflict

Rebase onto integration after 2A; coordinator verifies `roster.py` diff is parse-only.

## Verification subset

```bash
uv run pytest tests/unit/test_civix.py tests/unit/test_legacy.py -q
uv run pytest tests/verify -q
```

## Mechanical spec (from v1.0.0)

### RF-DRY-002 — One canonical `VoterRecord.from_csv_row`

**Files:** `models.py` (add classmethod); `civix.py:317-332, 524-543`; `roster.py:262-299`.

Three sites build a `VoterRecord` from a CSV row with subtle differences:

- `civix.py` calls `VoteMethod(row["VOTING_METHOD"])` (raises on unknown).
- `roster.py` lowercase-substring-matches `"MAIL"`, falls through to `IN_PERSON` (silently
  absorbs garbage).
- VUID normalization: civix uses `.zfill(10)`, legacy `_parse_county_csv` documented as
  bare `str()` but actually does `.zfill(10)` too (contradicts `AGENTS.md` Learned
  Workspace Facts — settle this here).

Add a single classmethod:

```python
class VoterRecord(BaseModel):
    @classmethod
    def from_csv_row(
        cls,
        row: dict[str, str],
        *,
        county: str,
        election_id: str,
        report_date: date,
    ) -> "VoterRecord":
        return cls(
            id_voter=row["ID_VOTER"].strip().zfill(10),
            voter_name=row.get("VOTER_NAME", "").strip(),
            precinct=row.get("PRECINCT", "").strip(),
            voting_method=_parse_voting_method(row["VOTING_METHOD"]),
            county=county,
            election_id=election_id,
            report_date=report_date,
        )
```

Update `AGENTS.md` Learned Workspace Facts to remove the now-stale "legacy uses `str()` only"
note. Both sources now `.zfill(10)`.
