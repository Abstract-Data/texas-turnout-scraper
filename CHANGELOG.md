# Changelog

All notable changes to this project are documented in this file.

Release versions and `pyproject.toml` are updated by [Release Please](https://github.com/googleapis/release-please)
when release PRs merge to `main`. Use [Conventional Commits](https://www.conventionalcommits.org/) on
`main` (for example `feat:`, `fix:`, `docs:`, `chore:`) so changelogs and semver bumps stay accurate.

## [0.2.9](https://github.com/Abstract-Data/texas-turnout-scraper/compare/texas-turnout-scraper-v0.2.8...texas-turnout-scraper-v0.2.9) (2026-10-29)


### Tests

* **verify:** pin EV_SYNC_MODE=apply for the ingest Worker ([cc22223](https://github.com/Abstract-Data/texas-turnout-scraper/commit/cc222231a5092acb4ab17ad2d7db821746fcf357))

## [0.2.8](https://github.com/Abstract-Data/texas-turnout-scraper/compare/texas-turnout-scraper-v0.2.7...texas-turnout-scraper-v0.2.8) (2026-10-28)


### Features

* **api:** default aggregates are election-to-date; every response says its semantics ([#69](https://github.com/Abstract-Data/texas-turnout-scraper/issues/69)) ([2b5f0a4](https://github.com/Abstract-Data/texas-turnout-scraper/commit/2b5f0a4fa06b89bd157cd196f4395d126dc7299c))
* **api:** elections list and roster routes (one day, all days) for roster tokens ([#74](https://github.com/Abstract-Data/texas-turnout-scraper/issues/74)) ([f61edd5](https://github.com/Abstract-Data/texas-turnout-scraper/commit/f61edd5b1f3ed52234028d5a4a62156e6c2dda44))
* **auth:** token scopes; only a roster-scoped token can read voter rows ([#73](https://github.com/Abstract-Data/texas-turnout-scraper/issues/73)) ([68f796f](https://github.com/Abstract-Data/texas-turnout-scraper/commit/68f796f8d4e3a9cd99ebe7ff7e6afc1c5578d2cf))
* **cli:** interactive, expiring API keys stored in 1Password (tx-turnout api) ([#91](https://github.com/Abstract-Data/texas-turnout-scraper/issues/91)) ([2b18bbe](https://github.com/Abstract-Data/texas-turnout-scraper/commit/2b18bbe4590f4b27a472fdebd683dec7260beb04))
* **cli:** write an agent handoff onto each issued API key ([#103](https://github.com/Abstract-Data/texas-turnout-scraper/issues/103)) ([5e23505](https://github.com/Abstract-Data/texas-turnout-scraper/commit/5e235055add45f292f41f1d31c23a606c73e3d70))
* **ev-api:** Worker cron dispatches the early-vote sync three times a day until Nov 3 ([#85](https://github.com/Abstract-Data/texas-turnout-scraper/issues/85)) ([09993ce](https://github.com/Abstract-Data/texas-turnout-scraper/commit/09993ceb9b838526a54258ff106797a63612216f))
* **ev-ingest:** Container image and Worker for the portal egress spike (ADR-017) ([#119](https://github.com/Abstract-Data/texas-turnout-scraper/issues/119)) ([4182320](https://github.com/Abstract-Data/texas-turnout-scraper/commit/418232004eb525419d802834f16923424bc52034))
* **ev-ingest:** scheduled early-vote ingest in the Container, shipped with EV_SYNC_MODE=off (ADR-017) ([629cd08](https://github.com/Abstract-Data/texas-turnout-scraper/commit/629cd08154120cb9905afe60c8efaac4823c5eae))
* **ev-ingest:** scheduled early-vote ingest in the Container, shipped with EV_SYNC_MODE=off (ADR-017) ([78e9de4](https://github.com/Abstract-Data/texas-turnout-scraper/commit/78e9de4162641d3d2b51fdb5117c0ef8490cfa34))
* **ev-sync:** Workflow ingest code and sync Worker package (ADR-014) ([#101](https://github.com/Abstract-Data/texas-turnout-scraper/issues/101)) ([cd75bc1](https://github.com/Abstract-Data/texas-turnout-scraper/commit/cd75bc1447796e99f1f14fadbdfdd860ee691cf9))
* **geo:** county, precinct and district points for turnout voters, and a precinct mapping ([#79](https://github.com/Abstract-Data/texas-turnout-scraper/issues/79)) ([fd6b67d](https://github.com/Abstract-Data/texas-turnout-scraper/commit/fd6b67de18b9468524c451c1a35850c45a5b31aa))
* **geo:** per-voter precinct, districts and residential points (ADR-015) ([#104](https://github.com/Abstract-Data/texas-turnout-scraper/issues/104)) ([0d01597](https://github.com/Abstract-Data/texas-turnout-scraper/commit/0d01597cd4048504c71a02826b686d92b0a61774))
* **geo:** precinct point and bounding box per roster precinct, served by a lookup endpoint ([#96](https://github.com/Abstract-Data/texas-turnout-scraper/issues/96)) ([4a45c1c](https://github.com/Abstract-Data/texas-turnout-scraper/commit/4a45c1c1c9e44886021858b1bb053a1a5eff55ff))
* **infra:** tx-turnout-ev-sync-preview Worker, Workflow, and cron repoint ([#102](https://github.com/Abstract-Data/texas-turnout-scraper/issues/102)) ([e14cc12](https://github.com/Abstract-Data/texas-turnout-scraper/commit/e14cc12f5db09f9b9e3d348cd2adaade47ba723f))
* **loader:** keep every version of a changed day in R2 (90 days) and log added/removed voters ([#64](https://github.com/Abstract-Data/texas-turnout-scraper/issues/64)) ([b6cce49](https://github.com/Abstract-Data/texas-turnout-scraper/commit/b6cce49e05331c2604e54339db766d9394c1a315))
* **loader:** store every roster column in D1 as validated SQLModel rows, with rejects quarantined ([#77](https://github.com/Abstract-Data/texas-turnout-scraper/issues/77)) ([5c1b126](https://github.com/Abstract-Data/texas-turnout-scraper/commit/5c1b126a61f9c80f3cdaa3f1c04b9190a95d7ed3))
* **loader:** write only the roster rows that changed; key rows by a small integer ([#78](https://github.com/Abstract-Data/texas-turnout-scraper/issues/78)) ([de67335](https://github.com/Abstract-Data/texas-turnout-scraper/commit/de67335328b73d4f5fa9eba7b3184fec1cda62b7))
* **repeats:** flag VUIDs counted more than once or removed, and store the non-first appearances ([#83](https://github.com/Abstract-Data/texas-turnout-scraper/issues/83)) ([f20a7ee](https://github.com/Abstract-Data/texas-turnout-scraper/commit/f20a7ee71766110df9defae5e832d5ad63a17e03))
* **roster:** Range resume and overlapped chunk reads for large day downloads ([#76](https://github.com/Abstract-Data/texas-turnout-scraper/issues/76)) ([d80f02e](https://github.com/Abstract-Data/texas-turnout-scraper/commit/d80f02e163bf4c9ac45a7ec74022cde63c593b40))
* **snapshots:** default to the shared R2 keys ([#67](https://github.com/Abstract-Data/texas-turnout-scraper/issues/67)) ([c253461](https://github.com/Abstract-Data/texas-turnout-scraper/commit/c2534619c2ae7818247d5d3800a1d0ef90dde4f8))
* **voters:** voterfile attributes, typed history, scores and an age view for turnout voters ([#80](https://github.com/Abstract-Data/texas-turnout-scraper/issues/80)) ([a23c599](https://github.com/Abstract-Data/texas-turnout-scraper/commit/a23c5998d8f5234301f11b9029b85b815395c579))


### Bug Fixes

* **aggregates:** publish distinct voters, restated days and county_fips ([#115](https://github.com/Abstract-Data/texas-turnout-scraper/issues/115)) ([03d28c9](https://github.com/Abstract-Data/texas-turnout-scraper/commit/03d28c984f217d1a65c350ff5d1acd40d5bb8946))
* **api:** issued keys carry the Cloudflare Access pair ([#111](https://github.com/Abstract-Data/texas-turnout-scraper/issues/111)) ([#114](https://github.com/Abstract-Data/texas-turnout-scraper/issues/114)) ([2a55f63](https://github.com/Abstract-Data/texas-turnout-scraper/commit/2a55f636206e88a42b50e9d3a3b8442cd9c9afc1))
* **ci:** a release bump no longer trips the preview Worker drift check ([#70](https://github.com/Abstract-Data/texas-turnout-scraper/issues/70)) ([a78b95a](https://github.com/Abstract-Data/texas-turnout-scraper/commit/a78b95a8ca2455612346f064aa18793b13ae4aed))
* **ev-api:** one auth dependency on every route; unknown token scopes fail closed ([#113](https://github.com/Abstract-Data/texas-turnout-scraper/issues/113)) ([8acbd52](https://github.com/Abstract-Data/texas-turnout-scraper/commit/8acbd52cf9dbc923b88ec03ba93561adb3a11395))
* **ev-ingest:** clear a run that cannot be tracked, and drop stale cron docs ([036714e](https://github.com/Abstract-Data/texas-turnout-scraper/commit/036714e791c5a95492b73a6bdc3b7c99031cfd48))
* **loader:** repoint the pointer when its day file is replaced ([#71](https://github.com/Abstract-Data/texas-turnout-scraper/issues/71)) ([182f29f](https://github.com/Abstract-Data/texas-turnout-scraper/commit/182f29f33887736e57b5e48643816b3a36f0cb4c))
* **loader:** scheduled runs re-hash every day; retry dropped downloads ([#63](https://github.com/Abstract-Data/texas-turnout-scraper/issues/63)) ([0f46a01](https://github.com/Abstract-Data/texas-turnout-scraper/commit/0f46a015104368ad2d2c46e0ba41212514dc2c23))
* **worker:** log GitHub dispatch failures with truncated body ([#98](https://github.com/Abstract-Data/texas-turnout-scraper/issues/98)) ([edc0127](https://github.com/Abstract-Data/texas-turnout-scraper/commit/edc0127656c4b787e31e251f4f65ca0f72862424))


### Performance

* **ingest:** an unchanged early-vote day costs one download and one parse ([#86](https://github.com/Abstract-Data/texas-turnout-scraper/issues/86)) ([f251e19](https://github.com/Abstract-Data/texas-turnout-scraper/commit/f251e1963ff717c7d8f0f0967d3d032d5a7b8244))


### Documentation

* **adr:** ADR-017 runs the early-vote loader in a Cloudflare Container ([#118](https://github.com/Abstract-Data/texas-turnout-scraper/issues/118)) ([f8a7b2d](https://github.com/Abstract-Data/texas-turnout-scraper/commit/f8a7b2d73feb09662513e5c3fd0d3b34ffc3fc6c))
* **adr:** propose the early-vote ingest as a Cloudflare Workflow (ADR-014) ([#89](https://github.com/Abstract-Data/texas-turnout-scraper/issues/89)) ([f7a58ec](https://github.com/Abstract-Data/texas-turnout-scraper/commit/f7a58ec51601e68a04819237a48b11d08dcb516d))


### Tests

* **geo:** pin a voter geo page under D1's 2 MB string limit; 256 pages ([#105](https://github.com/Abstract-Data/texas-turnout-scraper/issues/105)) ([a82bca5](https://github.com/Abstract-Data/texas-turnout-scraper/commit/a82bca54452c5e2c417f9fb7d5157ff1ae7b03c1))
* Hypothesis property and stateful tests for validation, boundaries and roster state ([#87](https://github.com/Abstract-Data/texas-turnout-scraper/issues/87)) ([c7e18a7](https://github.com/Abstract-Data/texas-turnout-scraper/commit/c7e18a7881da28f6346b5ca0ca0dd853469876ed))
* keep git's hook variables out of tests that build their own repo ([#107](https://github.com/Abstract-Data/texas-turnout-scraper/issues/107)) ([3c2a0ed](https://github.com/Abstract-Data/texas-turnout-scraper/commit/3c2a0edcf1e60e39e32bb653cd111c9c57640da3))
* stub pacing sleeps in unit tests and run them with pytest-xdist ([#121](https://github.com/Abstract-Data/texas-turnout-scraper/issues/121)) ([8d52ce3](https://github.com/Abstract-Data/texas-turnout-scraper/commit/8d52ce380d424ea3417443065fe4ff3118b86dd6))

## [0.2.7](https://github.com/Abstract-Data/texas-turnout-scraper/compare/texas-turnout-scraper-v0.2.6...texas-turnout-scraper-v0.2.7) (2026-10-23)


### Features

* **civix:** EVR walkthrough with opt-in county download, in the civix package ([#11](https://github.com/Abstract-Data/texas-turnout-scraper/issues/11)) ([#55](https://github.com/Abstract-Data/texas-turnout-scraper/issues/55)) ([d5b2a53](https://github.com/Abstract-Data/texas-turnout-scraper/commit/d5b2a53f4b541baa9b3595d8e36dd08b2933dd10))
* **claude-code:** Add Claude Code GitHub Workflow ([#52](https://github.com/Abstract-Data/texas-turnout-scraper/issues/52)) ([9d00859](https://github.com/Abstract-Data/texas-turnout-scraper/commit/9d0085996f603a316beceb4600978cf4047a65ff))
* **ev-api:** precompute D1 aggregates at loader write time (Option B) ([#48](https://github.com/Abstract-Data/texas-turnout-scraper/issues/48)) ([4a4cb54](https://github.com/Abstract-Data/texas-turnout-scraper/commit/4a4cb54a48ff0487a0a7901ec69eec7b07ee6b8e))
* **preview:** consumer bearer token auth (stacked on [#22](https://github.com/Abstract-Data/texas-turnout-scraper/issues/22)) ([#32](https://github.com/Abstract-Data/texas-turnout-scraper/issues/32)) ([7ec6737](https://github.com/Abstract-Data/texas-turnout-scraper/commit/7ec6737efb3efccea0deb3076d40d6205543fa7b))


### Bug Fixes

* **api:** invalid election id is 400; read client sends a bearer token; README matrix matches reality ([#60](https://github.com/Abstract-Data/texas-turnout-scraper/issues/60)) ([4e485c3](https://github.com/Abstract-Data/texas-turnout-scraper/commit/4e485c3eed64d508f0cbb2df7cfd6672aa3b6f0f))
* **backfill:** fail closed on unreadable aggregates, probe before paging, keep tzdata current ([#50](https://github.com/Abstract-Data/texas-turnout-scraper/issues/50) [#49](https://github.com/Abstract-Data/texas-turnout-scraper/issues/49) [#42](https://github.com/Abstract-Data/texas-turnout-scraper/issues/42)) ([#54](https://github.com/Abstract-Data/texas-turnout-scraper/issues/54)) ([d5321bd](https://github.com/Abstract-Data/texas-turnout-scraper/commit/d5321bd82bf0df3dbac4e7a5a8618d48f1ab381e))
* **data-refresh:** legacy pace kwarg and oversize roster push failures ([#57](https://github.com/Abstract-Data/texas-turnout-scraper/issues/57)) ([3f7524e](https://github.com/Abstract-Data/texas-turnout-scraper/commit/3f7524e0c07f90d312fd186bfa47e5c6fb5e78ff))
* **ev-api:** D1 row coercion for live aggregate reads + preview observability ([#45](https://github.com/Abstract-Data/texas-turnout-scraper/issues/45)) ([2980c67](https://github.com/Abstract-Data/texas-turnout-scraper/commit/2980c678f5785616daa74bdb123b9187dee592f5))
* **ev-api:** D1 run() for Python Worker aggregate reads ([#44](https://github.com/Abstract-Data/texas-turnout-scraper/issues/44)) ([d5b507f](https://github.com/Abstract-Data/texas-turnout-scraper/commit/d5b507f2dec1faf408086aace1d26385ca8cc011))
* **ev-api:** D1 SQL aggregates without loading full chunk payloads ([#47](https://github.com/Abstract-Data/texas-turnout-scraper/issues/47)) ([0db5b54](https://github.com/Abstract-Data/texas-turnout-scraper/commit/0db5b547dda17df8bd673a945c1612fa3db5dcbc))
* **ev-api:** Pyodide scrypt fallback for consumer Bearer auth ([#43](https://github.com/Abstract-Data/texas-turnout-scraper/issues/43)) ([55cc72d](https://github.com/Abstract-Data/texas-turnout-scraper/commit/55cc72de108ba23185f5efbdf89461ac5251f01a))
* **preview:** bundle tzdata for Pyodide ZoneInfo in Worker deploy ([#40](https://github.com/Abstract-Data/texas-turnout-scraper/issues/40)) ([1f8cf2c](https://github.com/Abstract-Data/texas-turnout-scraper/commit/1f8cf2c2c741c24e636ee81c5a701d3119262344))
* **preview:** commit Pyodide wheels and CI Worker import smoke ([#38](https://github.com/Abstract-Data/texas-turnout-scraper/issues/38)) ([954b8d3](https://github.com/Abstract-Data/texas-turnout-scraper/commit/954b8d3b3d31410835c65be5f2f66cb93f149d9f))
* **preview:** import ev-53815 D1 and wire EV_53815 worker binding ([#46](https://github.com/Abstract-Data/texas-turnout-scraper/issues/46)) ([adf3121](https://github.com/Abstract-Data/texas-turnout-scraper/commit/adf31211f1b96b87feb34c458da10bb6c03a8be5))
* **preview:** rotate the Access service token through Terraform ([#59](https://github.com/Abstract-Data/texas-turnout-scraper/issues/59)) ([3b337df](https://github.com/Abstract-Data/texas-turnout-scraper/commit/3b337dfcbf443b466a040bf78f085195d2c3659a))
* **preview:** route EV API Worker on custom domain behind Access ([#41](https://github.com/Abstract-Data/texas-turnout-scraper/issues/41)) ([3882242](https://github.com/Abstract-Data/texas-turnout-scraper/commit/388224228000350e7f7daade647afb9459ec3c88))
* **preview:** sync vendored voterfile.py so Terraform preview plans can run ([#58](https://github.com/Abstract-Data/texas-turnout-scraper/issues/58)) ([3e0051b](https://github.com/Abstract-Data/texas-turnout-scraper/commit/3e0051b7122aa62dfa5ec25ee9611ad0909463b5))
* **release:** bump the project version in uv.lock in the release PR; skip Claude review on release PRs ([#61](https://github.com/Abstract-Data/texas-turnout-scraper/issues/61)) ([f9fc908](https://github.com/Abstract-Data/texas-turnout-scraper/commit/f9fc9087c2e868a0fea6d3355dd5760135e74577))
* **security:** security hardening work ([#51](https://github.com/Abstract-Data/texas-turnout-scraper/issues/51)) ([f29f349](https://github.com/Abstract-Data/texas-turnout-scraper/commit/f29f349d1b710bcb863cbced0a52a6146c36d629))
* **verify:** ignore the package's own __version__ line in the vendored-bundle check ([#62](https://github.com/Abstract-Data/texas-turnout-scraper/issues/62)) ([b6998e3](https://github.com/Abstract-Data/texas-turnout-scraper/commit/b6998e38d669c2f1367e3de961a88bdba1c62ce0))

## [0.2.6](https://github.com/Abstract-Data/texas-turnout-scraper/compare/texas-turnout-scraper-v0.2.5...texas-turnout-scraper-v0.2.6) (2026-10-20)


### Features

* **civix:** add swappable early-vote read client ([#18](https://github.com/Abstract-Data/texas-turnout-scraper/issues/18)) ([a7d8295](https://github.com/Abstract-Data/texas-turnout-scraper/commit/a7d82954be4749e5b8e8246b001b6831b0c6242e))
* **early-vote:** Civix EVR_STATEWIDE roster sync to D1 (Phase 2) ([#29](https://github.com/Abstract-Data/texas-turnout-scraper/issues/29)) ([dbe5385](https://github.com/Abstract-Data/texas-turnout-scraper/commit/dbe5385a71068d4e1dcb4f8ed8f65d33d0eb43b8))
* **early-vote:** D1 VUID dup schema and pure roster compare ([#37](https://github.com/Abstract-Data/texas-turnout-scraper/issues/37)) ([cca9f79](https://github.com/Abstract-Data/texas-turnout-scraper/commit/cca9f79c4ed4e85ed6ec29ed092d31b285800d29))
* **ev-api:** preview aggregate read Worker ([#22](https://github.com/Abstract-Data/texas-turnout-scraper/issues/22)) ([e9233ff](https://github.com/Abstract-Data/texas-turnout-scraper/commit/e9233ff08bd9ba443a231e96d0b7700666f8b669))


### Bug Fixes

* **preview:** deep-review pywrangler drift + JS module MIME ([3881e6f](https://github.com/Abstract-Data/texas-turnout-scraper/commit/3881e6f0d72bf3f078e658e03a239a19a5288794))
* **preview:** vendor workers-runtime-sdk for Python Worker Terraform upload ([a488853](https://github.com/Abstract-Data/texas-turnout-scraper/commit/a4888538b8d1cc0fa8eaab381aaa17d928321795))
* **preview:** vendor workers-runtime-sdk for Python Worker upload ([bd66da5](https://github.com/Abstract-Data/texas-turnout-scraper/commit/bd66da5dde6869ab7303679ec91a125660265f22))

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
