# WS-3 — Audit pipeline unification (SERIAL)
# Version: 1.1.0 (workstream)
# Parent: [v1.1.0.md](../v1.1.0.md) · Manifest: [parallel-manifest.md](../parallel-manifest.md)

## Agent contract

| Field | Value |
|-------|-------|
| Branch | `feature/review-remediation/ws-3-audit-unify` |
| Base | `feature/review-remediation` (after Wave 2 gate) |
| Model | claude-opus-4-6 or claude-sonnet-4-6 |
| Wave | 3 |
| Exec | **SERIAL — single agent only** |

### File lock (MAY edit)

- `src/texas_turnout_scraper/audit.py`
- `src/texas_turnout_scraper/writer.py` (delete `audit_from_records`; keep CSV/accumulate)
- `src/texas_turnout_scraper/models.py` (`FindingType`, `AuditFinding`)
- `src/texas_turnout_scraper/cli.py` — audit routes only
- `src/texas_turnout_scraper/mcp_server.py` — `run_audit` only
- `tests/unit/test_audit.py`, `tests/unit/test_writer.py` (merge audit tests)
- `data/**/audit_ev_*.json` (regenerate or version-stamp)

### Forbidden (MUST NOT touch)

- Unrelated CLI fetch-all / protocol code (WS-4)
- `http_transport.py`

### Close issues

- RF-ARCH-001 (Critical)
- RF-CPLX-003

### Pre-confirmed vocabulary (do not re-debate)

`multiple_counties`, `conflicting_method`, `multiple_dates`, `name_mismatch`,
`precinct_mismatch`, `turnout_anomaly`, `missing_county`; `audit_schema_version: "2.0"`

### On conflict

Coordinator only; no parallel agents during Wave 3.

## Verification subset

```bash
uv run pytest tests/unit/test_audit.py tests/unit/test_writer.py -v
uv run pytest tests/verify -q
```

## Mechanical spec (from v1.0.0)

## Phase 3 — Unify the two audit pipelines (Day 3-4)

Goal: retire the divergent `audit.py` vs `writer.py::audit_from_records` pipelines (RF-ARCH-001
— Critical).

### Background

The two audit implementations emit **different `finding_type` strings for the same
condition** and detect **different sets of issues**. From the Refactoring report:

| Condition | `audit.py` | `writer.py` |
|---|---|---|
| Same VUID in multiple counties | `duplicate_vuid` | `multiple_counties` |
| Same VUID, IN-PERSON + MAIL-IN | `cross_method_duplicate` | `conflicting_method` |
| Same VUID on multiple dates | (not detected) | `multiple_dates` |
| Same VUID, mismatched name | (not detected) | `name_mismatch` |
| Same VUID, mismatched precinct | (not detected) | `precinct_mismatch` |
| Roster count > registered voters | `turnout_anomaly` | (not detected) |
| County in turnout but missing from roster | `missing_county` | (not detected) |

`cli.audit_run` routes through `writer.audit_from_records`; `cli.audit_run_inline` and
`mcp_server.run_audit` (after Phase 1) route through `audit.audit_from_csv` — so the audit
report a user gets depends on which subcommand they ran.

### Plan

1. **Vocabulary:** use the `writer.py` superset (confirmed). Canonical set:

   ```python
   class FindingType(str, Enum):
       MULTIPLE_COUNTIES = "multiple_counties"
       CONFLICTING_METHOD = "conflicting_method"
       MULTIPLE_DATES = "multiple_dates"
       NAME_MISMATCH = "name_mismatch"
       PRECINCT_MISMATCH = "precinct_mismatch"
       TURNOUT_ANOMALY = "turnout_anomaly"
       MISSING_COUNTY = "missing_county"
   ```

2. **Declare on the model:** `AuditFinding.finding_type: FindingType` (was `str`).

3. **One canonical entry point:**

   ```python
   def audit_records(
       records: Iterable[VoterRecord],
       *,
       turnout: Sequence[CountyTurnout] | None = None,
   ) -> AuditReport: ...
   ```

   Combine the 5 within-roster checks from `writer.audit_from_records` with the 2
   turnout-cross checks from `audit.audit_roster`. Single pass over records where
   possible.

4. **Delete** `writer.audit_from_records`. **Repoint** `cli.audit_run`,
   `cli.audit_run_inline`, `mcp_server.run_audit`.

5. **Stage the rename** in stored `audit_ev_*.json` files: add `"audit_schema_version":
   "2.0"` to the new format so downstream consumers can branch.

### RF-CPLX-003 — Split `audit_roster` into per-check helpers

After the consolidation, the resulting `audit_records` function will be long. Split into
private helpers:

```python
def _check_multiple_counties(records) -> list[AuditFinding]: ...
def _check_conflicting_methods(records) -> list[AuditFinding]: ...
def _check_multiple_dates(records) -> list[AuditFinding]: ...
def _check_name_mismatches(records) -> list[AuditFinding]: ...
def _check_precinct_mismatches(records) -> list[AuditFinding]: ...
def _check_turnout_anomalies(records, turnout) -> list[AuditFinding]: ...
def _check_missing_counties(records, turnout) -> list[AuditFinding]: ...
```

Each check function is independently testable and one screen long.

### Phase 3 verification

Both modules' test suites must merge into one cohesive `test_audit.py`. Existing
`audit_ev_*.json` fixtures must parse against the new schema (or be regenerated with
`audit_schema_version` 2.0).

**Coordinator:** bump `AGENTS.md` to **2.0.0** if audit schema ships without backward-compatible reader.
