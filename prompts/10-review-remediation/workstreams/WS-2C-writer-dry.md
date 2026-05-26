# WS-2C — CSV writer DRY
# Version: 1.1.0 (workstream)
# Parent: [v1.1.0.md](../v1.1.0.md) · Manifest: [parallel-manifest.md](../parallel-manifest.md)

## Agent contract

| Field | Value |
|-------|-------|
| Branch | `feature/review-remediation/ws-2c-writer-dry` |
| Base | `feature/review-remediation` |
| Model | claude-sonnet-4-6 |
| Wave | 2 |
| Exec | parallel (with 2D after 2A; independent of 2B if no writer overlap) |

### File lock (MAY edit)

- `src/texas_turnout_scraper/writer.py` (CSV writers only — not audit logic)
- `src/texas_turnout_scraper/voterfile.py` (CSV writers at `:560-591` only)

### Forbidden (MUST NOT touch)

- `audit.py`, `writer.audit_from_records` (WS-3)
- `accumulate_roster` perf (WS-5E)

### Close issues

- RF-DRY-004

### On conflict

Stop if diff touches audit functions.

## Verification subset

```bash
uv run pytest tests/unit/test_writer.py tests/unit/test_voterfile.py -q
```

## Mechanical spec (from v1.0.0)

### RF-DRY-004 — Extract `_write_dict_rows` for CSV writers

**Files:** `src/texas_turnout_scraper/writer.py:189-240`, `voterfile.py:560-591`.

`write_roster_csv` and `roster_csv_to_text` differ only in destination
(file path vs `StringIO`). `voterfile.write_enriched_csv` is the same shape with two extra
columns. Extract:

```python
def _voter_record_to_row(rec: VoterRecord) -> dict[str, str]: ...
def _enriched_record_to_row(rec: EnrichedVoterRecord) -> dict[str, str]: ...
def _write_dict_rows(fh, fieldnames, rows) -> None: ...
```

`roster_csv_to_text(records)` collapses to `write_roster_csv(records, io.StringIO()).getvalue()`.
