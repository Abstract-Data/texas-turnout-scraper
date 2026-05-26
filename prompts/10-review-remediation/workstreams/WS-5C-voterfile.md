# WS-5C — Voterfile DuckDB + detect_columns + bisect
# Version: 1.1.0 (workstream)
# Parent: [v1.1.0.md](../v1.1.0.md) · Manifest: [parallel-manifest.md](../parallel-manifest.md)

## Agent contract

| Field | Value |
|-------|-------|
| Branch | `feature/review-remediation/ws-5c-voterfile` |
| Base | `feature/review-remediation` (after WS-4C) |
| Model | claude-sonnet-4-6 |
| Wave | 5 |
| Exec | parallel |

### File lock (MAY edit)

- `src/texas_turnout_scraper/voterfile.py` (full module except unrelated to scope)

### Forbidden (MUST NOT touch)

- `writer.py` audit paths

### Close issues

- P3-SEC-001
- RF-SMELL-005
- RF-DRY-009

## Verification subset

```bash
uv run pytest tests/unit/test_voterfile.py -v
```

## Mechanical spec (from v1.0.0)

### P3-SEC-001 — DuckDB Python `read_csv` API instead of SQL-path interpolation

**File:** `src/texas_turnout_scraper/voterfile.py:383, 583`

Currently:

```python
vf_path_str = str(voterfile_path).replace("'", "''")
con.execute(f"... read_csv('{vf_path_str}', ...) ...")
```

Replace with:

```python
voterfile_view = duckdb.read_csv(str(voterfile_path), header=True, ...)
con.register("voterfile", voterfile_view)
con.execute("... FROM voterfile WHERE ID_VOTER IN ...")
```

The SQL-format surface for the path goes away entirely. Low risk in practice, clean fix.

### RF-SMELL-005 — Refactor `detect_columns` predicate loop

**File:** `src/texas_turnout_scraper/voterfile.py:194-240`

Replace the 3-level nested for/break with a predicate list:

```python
_STRATEGIES = [
    ("✓ Exact", lambda candidates, name: name if name in candidates else None),
    ("~ Prefix", lambda candidates, name: next((c for c in candidates if c.startswith(name)), None)),
    ("~ Substring", lambda candidates, name: next((c for c in candidates if name in c), None)),
]

for label, predicate in _STRATEGIES:
    match = predicate(candidates, target)
    if match is not None:
        return match, label
```

### RF-DRY-009 — `_AGE_BRACKETS` via `bisect`

**File:** `src/texas_turnout_scraper/voterfile.py:54-62`

Replace the linear scan with `bisect.bisect_right([24, 34, 44, 54, 64, 74], age)`.
