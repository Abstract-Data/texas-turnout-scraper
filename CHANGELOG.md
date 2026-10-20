# Changelog

All notable changes to this project are documented in this file.

Release versions and `pyproject.toml` are updated by [Release Please](https://github.com/googleapis/release-please)
when release PRs merge to `main`. Use [Conventional Commits](https://www.conventionalcommits.org/) on
`main` (for example `feat:`, `fix:`, `docs:`, `chore:`) so changelogs and semver bumps stay accurate.

## [0.2.5](https://github.com/Abstract-Data/texas-turnout-scraper/compare/texas-turnout-scraper-v0.2.4...texas-turnout-scraper-v0.2.5) (2026-10-20)


### Features

* **early-vote:** D1/KV writer with a hash gate that bumps the KV version only after a successful D1 write ([#21](https://github.com/Abstract-Data/texas-turnout-scraper/issues/21)) ([ca1a1c1](https://github.com/Abstract-Data/texas-turnout-scraper/commit/ca1a1c1b2996a04321a5dc389e8b1f46a19d471f))
* **infra:** preview Cloudflare stack for D1, KV, Worker, and Access ([#19](https://github.com/Abstract-Data/texas-turnout-scraper/issues/19)) ([07814c4](https://github.com/Abstract-Data/texas-turnout-scraper/commit/07814c42c46486e67cefa55fc4a07fb4450d005e))


### Bug Fixes

* **early-vote:** bump the KV version only after the pointer swap ([#28](https://github.com/Abstract-Data/texas-turnout-scraper/issues/28)) ([d2fd547](https://github.com/Abstract-Data/texas-turnout-scraper/commit/d2fd5477007c6933e2d0664e3bc5e08c58062272))
* **early-vote:** defer cleanup of stale file ids until the next write, from D1 state ([#28](https://github.com/Abstract-Data/texas-turnout-scraper/issues/28)) ([d2fd547](https://github.com/Abstract-Data/texas-turnout-scraper/commit/d2fd5477007c6933e2d0664e3bc5e08c58062272))
* **early-vote:** fail closed when a database on the [#21](https://github.com/Abstract-Data/texas-turnout-scraper/issues/21) schema has no file_id column ([#28](https://github.com/Abstract-Data/texas-turnout-scraper/issues/28)) ([d2fd547](https://github.com/Abstract-Data/texas-turnout-scraper/commit/d2fd5477007c6933e2d0664e3bc5e08c58062272))
* **early-vote:** keep early-vote sync to one in-flight run per election ([#28](https://github.com/Abstract-Data/texas-turnout-scraper/issues/28)) ([d2fd547](https://github.com/Abstract-Data/texas-turnout-scraper/commit/d2fd5477007c6933e2d0664e3bc5e08c58062272))
* **early-vote:** stage D1 chunks under a new file_id, then swap the pointer in one statement ([#28](https://github.com/Abstract-Data/texas-turnout-scraper/issues/28)) ([d2fd547](https://github.com/Abstract-Data/texas-turnout-scraper/commit/d2fd5477007c6933e2d0664e3bc5e08c58062272))

## [0.2.4](https://github.com/Abstract-Data/texas-turnout-scraper/compare/texas-turnout-scraper-v0.2.3...texas-turnout-scraper-v0.2.4) (2026-10-17)


### Features

* **civix:** installable ENR and archive package ([#17](https://github.com/Abstract-Data/texas-turnout-scraper/issues/17)) ([d6e038e](https://github.com/Abstract-Data/texas-turnout-scraper/commit/d6e038eddb51ecbaa2435d51dfbc496fae0e6a58))


### Bug Fixes

* **ci:** satisfy ruff 0.17 format and ty 0.0.87 warn-only exit ([cdac19f](https://github.com/Abstract-Data/texas-turnout-scraper/commit/cdac19f6d6b5fbbfe9a888927a96b5edc139f323))


### Documentation

* add security policy and grouped Dependabot updates ([#12](https://github.com/Abstract-Data/texas-turnout-scraper/issues/12)) ([e0a0e90](https://github.com/Abstract-Data/texas-turnout-scraper/commit/e0a0e9020c5ea31a3690c676d9e93f8bce239aaa))

## [0.2.3](https://github.com/Abstract-Data/texas-turnout-scraper/compare/texas-turnout-scraper-v0.2.2...texas-turnout-scraper-v0.2.3) (2026-05-29)


### Features

* **civix:** add election-day statewide voter roster fetch and parse ([#9](https://github.com/Abstract-Data/texas-turnout-scraper/issues/9)) ([416c973](https://github.com/Abstract-Data/texas-turnout-scraper/commit/416c9730de3f737a36591135ef59e0b072e98dde))

## [0.2.2](https://github.com/Abstract-Data/texas-turnout-scraper/compare/texas-turnout-scraper-v0.2.1...texas-turnout-scraper-v0.2.2) (2026-05-27)


### Features

* **cli:** add election day turnout fetch and storage for civix ([e63b6ad](https://github.com/Abstract-Data/texas-turnout-scraper/commit/e63b6adac229ddeebec8b27a84a76fced2645046))
* **elections:** add civix 58315 early voting turnout snapshot for 2026-05-26 ([9434c6a](https://github.com/Abstract-Data/texas-turnout-scraper/commit/9434c6a2d9241d5267ee4c5e78401bfb44419f22))


### Bug Fixes

* **data:** update turnout and roster records for election 58315 (2026-05-20/21) ([456306e](https://github.com/Abstract-Data/texas-turnout-scraper/commit/456306e45b14de80cdb45db899ca0e22873497b3))
* **data:** update turnout and roster records for election 58315 (2026-05-20/21) ([aadd9b7](https://github.com/Abstract-Data/texas-turnout-scraper/commit/aadd9b7587ae42d44dde6710d6b4f06e2af194d8))

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
