# WS-5D — Civix Pydantic fast paths
# Version: 1.1.0 (workstream)
# Parent: [v1.1.0.md](../v1.1.0.md) · Manifest: [parallel-manifest.md](../parallel-manifest.md)

## Agent contract

| Field | Value |
|-------|-------|
| Branch | `feature/review-remediation/ws-5d-civix-perf` |
| Base | `feature/review-remediation` (after WS-5B merged) |
| Model | claude-sonnet-4-6 |
| Wave | 5 |
| Exec | sequential[after: 5B] on `civix.py` |

### File lock (MAY edit)

- `src/texas_turnout_scraper/civix.py`

### Forbidden (MUST NOT touch)

- `models.py` (WS-5B)

### Close issues

- P3-PERF-001

## Verification subset

```bash
uv run pytest tests/unit/test_civix.py -v
```

## Mechanical spec (from v1.0.0)

### P3-PERF-001 — Pydantic v2 fast paths

**Files:** `src/texas_turnout_scraper/civix.py:185, 325-336`

For large payloads:

```python
# Was: data_dict = json.loads(raw_bytes.decode("utf-8")); CivixElection(**data_dict)
elections = TypeAdapter(list[CivixElection]).validate_json(raw_bytes)

# Was: [VoterRecord(**row) for row in csv.DictReader(text)]
adapter = TypeAdapter(list[VoterRecord])  # module-level
records = adapter.validate_python([_normalize_row(r) for r in csv.DictReader(text)])
```

Measure with `pytest --benchmark-only` (if you add `pytest-benchmark`); not blocking.
