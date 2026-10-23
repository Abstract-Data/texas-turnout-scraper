# Civix EVR walkthrough

`src/texas_turnout_scraper/civix/walkthrough.py` drives the Civix EVR early-vote UI
(`https://goelect.txelections.civixapps.com/ivis-evr-ui/evr`) with Playwright. It is
adapted from the Quick Results recorder `backend/scripts/record_civix_ui.py`
(revision `bae921b9`) and does not replay any ENR selectors, HTML, or traces.

The walk selects one election by `source_election_id` (a string, `56181` by default),
picks an early-voting date, and stops on the county turnout table. With
`--download-county <COUNTY>` it also clicks that county's "Voter Details Report" CSV
download. That file is the early-vote / by-mail file. The run records only byte count,
row count, SHA-256, and header field names, then deletes the file unless
`--keep-download` is passed. Statewide reports stay blocked.

## Run it (manual, live)

```bash
uv run --with playwright python -m playwright install chromium
uv run --with playwright python -m texas_turnout_scraper.civix.walkthrough \
    --headless --channel "" --download-county GALVESTON \
    --video-out docs/walkthroughs/civix-evr-walkthrough.mp4
```

Recordings go to `./.recordings/`, which is gitignored. They can hold voter data when
`--keep-download` is used. CI never hits the live site. The unit tests render a
synthetic page from `tests/fixtures/civix_evr/` and skip when Playwright is missing.

## Committed evidence

- `civix-evr-walkthrough.mp4`: 18 s desktop recording of the walk for election 56181,
  date 04/20/2026, ending on the aggregate turnout table with the GALVESTON download
  highlighted. No voter rows appear on screen.
- `civix-evr-download.json`: counts and field names for that county file (26 rows).
  Recorded 2026-10-22.
