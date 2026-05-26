# Changelog

All notable changes to this project are documented in this file.

Release versions and `pyproject.toml` are updated by [Release Please](https://github.com/googleapis/release-please)
when release PRs merge to `main`. Use [Conventional Commits](https://www.conventionalcommits.org/) on
`main` (for example `feat:`, `fix:`, `docs:`, `chore:`) so changelogs and semver bumps stay accurate.

## [0.2.0](https://github.com/Abstract-Data/texas-result-scraper/releases/tag/v0.2.0) (2026-05-24)

### Features

- httpx-based Texas SOS early-voting scraper with Pydantic v2 models, Typer CLI (`tx-turnout`), and FastMCP server
- Civix and legacy data paths with scheduled `data-refresh` workflow
