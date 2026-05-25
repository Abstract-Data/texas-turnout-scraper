# Guardrails — texas-turnout-scraper

Non-negotiable constraints for all contributors and AI agents working on this project.

---

## NEVER

### Session & HTTP
- **Never cold-POST the SOS report endpoints.** Always establish a `JSESSIONID` session first via the GET → POST → POST flow documented in `EARLY_VOTING_ROSTER.md`.
- **Never use Selenium** for this project. Use httpx (tests) or cloudscraper (production WAF bypass).
- **Never buffer the bulk ZIP in memory.** Stream to disk and unzip (`Strategy B`).
- **Never make more than 1 request per second** in the Strategy A county loop.

### Data Types
- **Never coerce `source_election_id` to int.** It is always a string (e.g. `"49664"`).
- **Never coerce `ID_VOTER` to int.** It is always a 10-digit string — leading zeros matter.
- **Never use `election_utils`.** That dependency is removed from this project.

### PII
- **Never log `VOTER_NAME` or `ID_VOTER` values.** These are PII even though they are public record.
- **Never include raw voter names or VUIDs in exception messages, tracebacks, or console output.**
- **Never commit real voter PII in test fixtures.** Synthesize or redact.

### Models
- **Never use SQLModel or SQLAlchemy.** Models are Pydantic v2, database-agnostic.
- **Never import from `election_utils`.** Build models fresh from `enums.py` and `models.py`.

### Git
- **Never force-push to `main`.** Use feature branches and PRs.
- **Never commit `.env` files.**
- **Never commit test fixtures with real voter data.**

### Data Files
- **Never remove `!data/**/*.csv` from `.gitignore`.** That exception is intentional — data files must be committed to serve the GitHub Pages static API.

---

## ALWAYS

- Always read `docs/ARCHITECTURE_SPEC.md` before making structural changes.
- Always read `docs/EARLY_VOTING_ROSTER.md` before touching the HTTP session or endpoint flow.
- Always keep `ID_VOTER` and `source_election_id` as strings throughout the pipeline.
- Always pace Strategy A at ≥1.0 s between county requests.
- Always use a real CSV parser (`csv.DictReader`) for SOS roster files.
- Always write `HANDOFF.md` at the end of a session (it is gitignored — local only).
- Always run `ruff check . --fix && ruff format .` before committing.
- Always run `pytest tests/unit -q` before committing.

---

## Definition of Done

A feature or fix is done when:

1. All unit tests pass (`pytest tests/unit -q`)
2. Ruff lint is clean (`ruff check . --select E,W,F --quiet`)
3. Ruff format passes (`ruff format . --check`)
4. The change is documented (docstring, or relevant docs/ file updated)
5. No PII is logged or committed
6. ID fields remain strings throughout
