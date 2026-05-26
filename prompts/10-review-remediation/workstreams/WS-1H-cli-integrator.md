# WS-1H — CLI integrator (Wave 1 serial)
# Version: 1.1.0 (workstream)
# Parent: [v1.1.0.md](../v1.1.0.md) · Manifest: [parallel-manifest.md](../parallel-manifest.md)

## Agent contract

| Field | Value |
|-------|-------|
| Branch | `feature/review-remediation/ws-1h-cli-integrator` |
| Base | `feature/review-remediation` (after 1F + 1G merged) |
| Model | claude-sonnet-4-6 |
| Wave | 1 |
| Exec | sequential[after: 1F, 1G] |

### File lock (MAY edit)

- `src/texas_turnout_scraper/cli.py` (**sole Wave-1 owner**)
- `src/texas_turnout_scraper/mcp_server.py` — in_person/mail count call sites only
- `src/texas_turnout_scraper/roster.py:103` — only if not deferred to WS-2A (prefer defer)

### Forbidden (MUST NOT touch)

- `mcp_server.py` keyword-arg fixes (WS-1A)
- `audit.py`, `writer.py`, `session.py` encapsulation

### Close issues

- RF-DRY-005
- RF-DRY-007 (CLI duplicate removal)
- RF-DRY-003 (call sites)

### On conflict

Coordinator resolves `cli.py` here; no other workstream may edit `cli.py` until WS-4C.

## Verification subset

**Full Phase 1 matrix (Wave 1 gate):**

```bash
uv run ruff check . --fix && uv run ruff format .
uv run ty check
uv run pytest tests/unit -q
uv run pytest tests/verify -q
```

## Mechanical spec (from v1.0.0)

### RF-DRY-007 — Centralize the `max(pace, 1.0)` floor (CLI duplicates)

Drop the 5 CLI `max(pace, 1.0)` duplicates at `cli.py:774, 902, 1131, 1309` now that
WS-1F enforced the floor in `LegacySession.__init__` and `CivixClient.__init__`.

### RF-DRY-005 — Sweep nested `import datetime` blocks in `cli.py`

**File:** `src/texas_turnout_scraper/cli.py:80, 86, 132, 150, 324, 1467, 1474, 1683-1684`

The top-level import `from datetime import date, datetime` is shadowed inside 8 helpers
by `import datetime` (the module). Inside those helpers, `datetime` refers to the module,
not the class — silent shadowing hazard.

Replace the file-level import with:

```python
import datetime as dt
```

Use `dt.datetime`, `dt.timezone`, `dt.timedelta`, `dt.date` throughout. Drop every nested
`import datetime`. Add a single `_iso_now()` helper:

```python
def _iso_now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
```

### RF-DRY-003 — Use `in_person_count` / `mail_in_count` at call sites

**Files:** `cli.py:730-731`, `mcp_server.py:155-156, 348-349`

Replace duplicated sums with `roster.in_person_count` / `roster.mail_in_count` (properties
from WS-1G).

### Phase 1 verification

All commands in **Verification subset** must pass before Wave 2 dispatch.

**Coordinator:** update `AGENTS.md` Learned Workspace Facts if pacing or datetime invariants changed.
