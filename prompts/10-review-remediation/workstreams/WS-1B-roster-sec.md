# WS-1B — Roster CSV security (bare except)
# Version: 1.1.0 (workstream)
# Parent: [v1.1.0.md](../v1.1.0.md) · Manifest: [parallel-manifest.md](../parallel-manifest.md)

## Agent contract

| Field | Value |
|-------|-------|
| Branch | `feature/review-remediation/ws-1b-roster-sec` |
| Base | `feature/review-remediation` |
| Model | claude-sonnet-4-6 |
| Wave | 1 |
| Exec | parallel |

### File lock (MAY edit)

- `src/texas_turnout_scraper/roster.py` (`_parse_county_csv` except block)
- Other `src/**/*.py` files **only** for `grep -rn 'except Exception' src/` fixes (exclude `cli.py`)

### Forbidden (MUST NOT touch)

- `cli.py`
- `session.py`

### Close issues

- P1-SEC-001

### On conflict

Stop on `roster.py` conflict with WS-2A/2B; coordinator serializes 1B → 2A → 2B.

## Verification subset

```bash
uv run ruff check src/texas_turnout_scraper/roster.py
uv run pytest tests/unit/test_legacy.py -q
grep -rn 'except Exception' src/  # expect zero or documented exceptions only
```

## Mechanical spec (from v1.0.0)

### P1-SEC-001 — Replace bare `except Exception` in `_parse_county_csv`

**File:** `src/texas_turnout_scraper/roster.py:319-326`

Two violations rolled into one block:

1. Bare `except Exception` — explicit `AGENTS.md` NEVER DO.
2. `exc_info=True` on a CSV-parse error — the traceback can include row contents (voter
   names, VUIDs), which is a PII-leak path.

Replace with:

```python
except (csv.Error, ValueError, KeyError) as exc:
    logger.warning(
        "Failed to parse CSV for county_id=%s (election %s, date %s) — %s.",
        county_id, source_election_id, ev_date,
        type(exc).__name__,
    )
    return None
```

Sweep the rest of the codebase for other bare-except blocks (`grep -rn 'except Exception' src/`)
and apply the same treatment.

**Do not** change `roster.py:103` pace mutation — deferred to WS-2A (RF-SMELL-001).
