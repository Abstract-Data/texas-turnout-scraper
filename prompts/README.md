# Prompts — texas-turnout-scraper Version Registry

This file is the **version registry** for all agent prompts in this project.
One `##` section per prompt. Each prompt lives in its own numbered subdirectory with:
- `current.md` — active version (starts with `# Version: X.Y.Z` header)
- `v{X.Y.Z}.md` — versioned snapshot (required — not stub-only)
- `.gitkeep`

**Versioning rules:** MAJOR = behavior change or full rewrite; MINOR = new tool/guardrail/scope change; PATCH = wording tweak.

**Suggested execution order:**
```
00 (optional) → 01 → 02 + 03 + 04 (parallel) → 05 → 06 → 07 → 08
09 (voterfile-match) is standalone — run after uv sync installs duckdb
```

> **Cleanup needed:** Run `git rm prompts/0*.md` and `git rm -r prompts/test-fixtures prompts/unit-tests-civix prompts/unit-tests-legacy prompts/unit-tests-writer prompts/integration-tests prompts/cli-fetch-all prompts/data-refresh-workflow prompts/cleanup-and-verification prompts/voterfile-match prompts/project-alignment` locally to remove the deprecated unnumbered directories.

---

## 00-project-alignment

**Current version:** 1.0.0
**Model:** claude-sonnet-4-6
**Last updated:** 2026-05-24
**Path:** `prompts/00-project-alignment/current.md`
**Purpose:** Run a full project-alignment audit against Abstract Data standards. Optional — run before any implementation work to verify the repo is structurally sound.
**Depends on:** —

### Changelog
- `1.0.0` (2026-05-24) — Initial version

---

## 01-test-fixtures

**Current version:** 1.0.0
**Model:** claude-sonnet-4-6
**Last updated:** 2026-05-24
**Path:** `prompts/01-test-fixtures/current.md`
**Purpose:** Create 9 synthetic fixture files for all unit tests. No real PII. Required before running prompts 02 and 03.
**Depends on:** —

### Changelog
- `1.0.0` (2026-05-24) — Initial version

---

## 02-unit-tests-civix

**Current version:** 1.0.0
**Model:** claude-sonnet-4-6
**Last updated:** 2026-05-24
**Path:** `prompts/02-unit-tests-civix/current.md`
**Purpose:** respx-mocked unit tests for `civix.py`. No live network.
**Depends on:** 01-test-fixtures

### Changelog
- `1.0.0` (2026-05-24) — Initial version; updated for new VoterRecord fields (county, election_id, report_date, voter_name)

---

## 03-unit-tests-legacy

**Current version:** 1.0.0
**Model:** claude-sonnet-4-6
**Last updated:** 2026-05-24
**Path:** `prompts/03-unit-tests-legacy/current.md`
**Purpose:** Unit tests for `session.py`, `elections.py`, `roster.py`, `turnout.py`.
**Depends on:** 01-test-fixtures

### Changelog
- `1.0.0` (2026-05-24) — Initial version

---

## 04-unit-tests-writer

**Current version:** 1.0.0
**Model:** claude-sonnet-4-6
**Last updated:** 2026-05-24
**Path:** `prompts/04-unit-tests-writer/current.md`
**Purpose:** Verify/extend `test_writer.py` — all 5 duplicate types, CSV round-trip, PII guard. `test_writer.py` is pre-written; prompt confirms all tests pass.
**Depends on:** —

### Changelog
- `1.0.0` (2026-05-24) — Initial version; `test_writer.py` pre-written, prompt verifies coverage

---

## 05-integration-tests

**Current version:** 1.0.0
**Model:** claude-sonnet-4-6
**Last updated:** 2026-05-24
**Path:** `prompts/05-integration-tests/current.md`
**Purpose:** Live-API integration tests gated behind `--live` pytest marker (skipped in CI). Requires network access to SOS portal.
**Depends on:** —

### Changelog
- `1.0.0` (2026-05-24) — Initial version

---

## 06-cli-fetch-all

**Current version:** 1.0.0
**Model:** claude-sonnet-4-6
**Last updated:** 2026-05-24
**Path:** `prompts/06-cli-fetch-all/current.md`
**Purpose:** Add `civix fetch-all` + `legacy fetch-all` CLI commands that combine all EV dates into one per-election roster file.
**Depends on:** —

### Changelog
- `1.0.0` (2026-05-24) — Initial version

---

## 07-data-refresh-workflow

**Current version:** 1.0.0
**Model:** claude-sonnet-4-6
**Last updated:** 2026-05-24
**Path:** `prompts/07-data-refresh-workflow/current.md`
**Purpose:** Update `.github/workflows/data-refresh.yml` for new CLI + one-file-per-election output.
**Depends on:** 06-cli-fetch-all

### Changelog
- `1.0.0` (2026-05-24) — Initial version

---

## 08-cleanup-and-verification

**Current version:** 1.0.0
**Model:** claude-sonnet-4-6
**Last updated:** 2026-05-24
**Path:** `prompts/08-cleanup-and-verification/current.md`
**Purpose:** Delete dead code (`results_scraper.py`), final lint pass, full test suite verification. Run last.
**Depends on:** all above

### Changelog
- `1.0.0` (2026-05-24) — Initial version

---

## 09-voterfile-match

**Current version:** 1.0.0
**Model:** claude-sonnet-4-6
**Last updated:** 2026-05-24
**Path:** `prompts/09-voterfile-match/current.md`
**Purpose:** Unit tests for `voterfile.py` — column detection, age brackets, DuckDB match logic, CSV round-trip, mapping persistence. Requires `uv sync` to install duckdb first.
**Depends on:** —

### Changelog
- `1.0.0` (2026-05-24) — Initial version
