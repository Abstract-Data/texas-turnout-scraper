# data-refresh-workflow — GitHub Actions Data Refresh
# Version: 1.0.0
# Model: claude-sonnet-4-6
# Last Updated: 2026-05-24
# Maintainer: John Eakin

# Prompt 07 — GitHub Actions: data-refresh workflow

## Goal
Update `.github/workflows/data-refresh.yml` to use the new CLI + one-file-per-election
output pattern. The workflow should run on a schedule, fetch all available elections,
and commit any changed CSV/JSON files to `main`.

## Files to read first (required context)
- `.github/workflows/data-refresh.yml` (existing, may need full replacement)
- `.github/workflows/ci.yml`
- `pyproject.toml` (package name, scripts)
- `src/texas_turnout_scraper/cli.py`
- `docs/ARCHITECTURE_SPEC.md`

## Files to modify
- `.github/workflows/data-refresh.yml`

## Files NOT to touch
- `.github/workflows/ci.yml`
- Any source module
- Any test file

## Workflow specification

### Trigger
```yaml
on:
  schedule:
    - cron: '0 8 * * *'   # 8:00 UTC daily (3 AM CT)
  workflow_dispatch:        # Allow manual runs from GitHub UI
    inputs:
      election_id:
        description: 'Specific election ID to refresh (leave blank for all)'
        required: false
        default: ''
      source:
        description: 'Data source to refresh'
        required: false
        default: 'all'
        type: choice
        options: ['all', 'civix', 'legacy']
```

### Jobs

#### `refresh-civix`
```yaml
jobs:
  refresh-civix:
    runs-on: ubuntu-latest
    permissions:
      contents: write   # needed to push data commits
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
      - name: Install package
        run: pip install -e ".[dev]"
      - name: Refresh Civix elections
        run: |
          # If specific election_id passed via dispatch, fetch only that one
          if [ -n "${{ github.event.inputs.election_id }}" ]; then
            tx-turnout civix fetch-all "${{ github.event.inputs.election_id }}"
          else
            # Discover and fetch all certified elections
            tx-turnout civix refresh-all
          fi
      - name: Commit data changes
        uses: stefanzweifel/git-auto-commit-action@v5
        with:
          commit_message: "data: refresh civix elections [skip ci]"
          file_pattern: "data/elections/civix/**"
```

#### `refresh-legacy`
Similar structure to `refresh-civix` but using `tx-turnout legacy ...` commands.

### New CLI command needed: `civix refresh-all`

This prompt also requires adding a `tx-turnout civix refresh-all` command to `cli.py` that:
1. Calls `CivixClient().list_elections()` to discover all elections
2. Filters to certified elections only
3. Calls `fetch-all` logic for each (skip elections where data is already current)
4. Prints a summary of elections updated

Freshness check: if `data/elections/civix/{id}/roster_ev_{id}.csv` exists and was
modified within the last 24 hours, skip that election.

### Data directory structure after refresh

```
data/
└── elections/
    ├── index.json                    # updated by workflow
    ├── civix/
    │   ├── 53813/
    │   │   ├── roster_ev_53813.csv   # one file per election, all dates combined
    │   │   └── audit_ev_53813.json   # audit report (if --audit flag set)
    │   └── 56181/
    │       └── roster_ev_56181.csv
    └── legacy/
        └── 49664/
            └── roster_ev_49664.csv
```

### `index.json` update

After each refresh, update `data/elections/index.json` with:
```json
{
  "last_updated": "2026-05-24T08:00:00Z",
  "civix": {
    "elections": [
      {
        "source_election_id": "53813",
        "election_name": "2026 REPUBLICAN PRIMARY ELECTION",
        "election_date": "2026-03-03",
        "election_type": "primary",
        "certified": true,
        "roster_path": "civix/53813/roster_ev_53813.csv",
        "last_refreshed": "2026-05-24T08:00:00Z",
        "total_records": 14521,
        "unique_vuids": 14500,
        "duplicate_vuid_count": 21
      }
    ]
  },
  "legacy": {
    "elections": []
  }
}
```

## Constraints
- Workflow must use `[skip ci]` in commit message to avoid triggering CI on data commits
- Never commit `.env` files or credentials
- Use `stefanzweifel/git-auto-commit-action@v5` for the commit step
- Pacing is enforced by the CLI — no need for `sleep` in the workflow
- If all fetches fail, the workflow should exit non-zero (don't silently commit nothing)

## Acceptance criteria
```bash
# Validate YAML syntax
python -c "import yaml; yaml.safe_load(open('.github/workflows/data-refresh.yml'))"
# Should not raise

# Workflow visible in GitHub Actions UI
# Manual dispatch works from UI with election_id input
```
