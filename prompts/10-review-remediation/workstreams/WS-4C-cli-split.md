# WS-4C — CLI package split (SERIAL)
# Version: 1.1.0 (workstream)
# Parent: [v1.1.0.md](../v1.1.0.md) · Manifest: [parallel-manifest.md](../parallel-manifest.md)

## Agent contract

| Field | Value |
|-------|-------|
| Branch | `feature/review-remediation/ws-4c-cli-split` |
| Base | `feature/review-remediation` (after 4A, 4B, 3) |
| Model | claude-opus-4-6 |
| Wave | 4 |
| Exec | sequential[after: 4A, 4B, 3] |

### File lock (MAY edit)

- `src/texas_turnout_scraper/cli/` package (new)
- `src/texas_turnout_scraper/cli.py` → thin mounts
- `tests/unit/test_cli_*.py`

### Forbidden (MUST NOT touch)

- `mcp_server.py` beyond import paths
- `audit.py` logic (WS-3 done)

### Close issues

- RF-DRY-001 (CLI collapse)
- RF-CPLX-001
- RF-DRY-008

### On conflict

Coordinator sole owner of `cli.py` structural changes after WS-1H.

## Verification subset

```bash
uv run pytest tests/unit -q
uv run pytest tests/integration --live -q   # optional spot-check, network
```

**Coordinator:** bump `AGENTS.md` to **1.3.0** at end of this workstream.

## Mechanical spec (from v1.0.0)

### Collapse the four CLI functions

**Files:** `src/texas_turnout_scraper/cli.py:759-882` (`civix_fetch_all`),
`:1095-1278` (`legacy_fetch_all`), `:884-965` (`civix_refresh_all`),
`:1280-1372` (`legacy_refresh_all`), `:236-270` (`_build_civix_index_entries`),
`:273-308` (`_build_legacy_index_entries`).

Replace with:

```python
def _fetch_all_impl(source: RosterSource, election_id: str, ...) -> int: ...
def _refresh_all_impl(source: RosterSource, ...) -> int: ...
def _build_index_entries(
    output_dir: Path,
    elections: Sequence[ElectionSummary],
    source_prefix: str,
    election_date_fn: Callable[[ElectionSummary], date],
) -> list[IndexEntry]: ...
```

Each Typer subcommand becomes a 5-line dispatcher:

```python
@civix_app.command("fetch-all")
def civix_fetch_all(election_id: str, ...):
    return _fetch_all_impl(CivixSource(...), election_id, ...)
```

### RF-CPLX-001 — Decompose oversized CLI commands

Extract three helpers per long command — `_resolve_inputs()`, `_run_pipeline()`,
`_render_summary()`. Move the interactive prompt block (~50 lines in `voterfile_match`)
into `cli/_interactive.py`. The Typer-decorated functions stay as thin shells.

After this, `cli.py` should be ~900 lines (target from the Refactoring report) and split
across `cli/civix.py`, `cli/legacy.py`, `cli/audit.py`, `cli/voterfile.py`,
`cli/_interactive.py`, `cli/_index.py`. Use a `cli/__init__.py` that mounts the
sub-apps.

### RF-DRY-008 — `_print_table` helper

The `'-' * len(header)` underline pattern appears at `cli.py:441, 647, 988, 1025`. Factor
into a single `_print_table(headers: Sequence[str], rows: Sequence[Sequence[str]])` helper.

### Phase 4 verification

`tests/unit/test_cli_fetch_all.py` and `test_cli_refresh.py` need to be re-pointed at the
protocol implementations. The adapter pattern keeps signatures stable for downstream
consumers.
