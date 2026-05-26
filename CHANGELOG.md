# Changelog

All notable changes to this project are documented in this file.

Release versions and `pyproject.toml` are updated by [Release Please](https://github.com/googleapis/release-please)
when release PRs merge to `main`. Use [Conventional Commits](https://www.conventionalcommits.org/) on
`main` (for example `feat:`, `fix:`, `docs:`, `chore:`) so changelogs and semver bumps stay accurate.

## [0.2.1](https://github.com/Abstract-Data/texas-turnout-scraper/compare/texas-turnout-scraper-v0.2.0...texas-turnout-scraper-v0.2.1) (2026-05-26)


### Bug Fixes

* close ed256928 review findings for audit and sources ([a89a1df](https://github.com/Abstract-Data/texas-turnout-scraper/commit/a89a1dffe386cac69dca6b430be4f410ac60efb4))


### Code Refactoring

* standardize type hints and improve code formatting ([c92bf7b](https://github.com/Abstract-Data/texas-turnout-scraper/commit/c92bf7b5b07f598f04453aff2681a10569321d70))


### Documentation

* add parallel review remediation workstreams and supporting infrastructure ([4f34f7e](https://github.com/Abstract-Data/texas-turnout-scraper/commit/4f34f7e034180738a7d4241a0ab65c9a18029d5b))
* add prompt 10 review-remediation workstreams and manifest ([038f07a](https://github.com/Abstract-Data/texas-turnout-scraper/commit/038f07afe4bc9e6e9fda52fc6e10225915612d25))
* add skill guides for mcp-tool-testing and dual-source-pattern ([1103e69](https://github.com/Abstract-Data/texas-turnout-scraper/commit/1103e6915d062e8b5007cb6c21a237f107834e3b))
* add testing guide, unit test prompts, and test fixtures ([#1](https://github.com/Abstract-Data/texas-turnout-scraper/issues/1)) ([8a014ea](https://github.com/Abstract-Data/texas-turnout-scraper/commit/8a014ea1bf02517309b470cd2413487b737b2679))


### Tests

* **cli:** handle requests http errors in civix fetch with retry logic ([#2](https://github.com/Abstract-Data/texas-turnout-scraper/issues/2)) ([4611707](https://github.com/Abstract-Data/texas-turnout-scraper/commit/4611707fa07377898e701337cb885d80e6eca02e))
* **gap_analysis:** add comprehensive unit tests and fixture data for turnout vs roster gap analysis ([13a14e7](https://github.com/Abstract-Data/texas-turnout-scraper/commit/13a14e73ed7d0efc1e374936207e5f740513731d))

## [0.2.0](https://github.com/Abstract-Data/texas-result-scraper/releases/tag/v0.2.0) (2026-05-24)

### Features

- httpx-based Texas SOS early-voting scraper with Pydantic v2 models, Typer CLI (`tx-turnout`), and FastMCP server
- Civix and legacy data paths with scheduled `data-refresh` workflow
