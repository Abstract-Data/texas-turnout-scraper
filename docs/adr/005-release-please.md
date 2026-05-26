# ADR-005: Automated releases with Release Please

**Date:** 2026-05-25
**Status:** Accepted

## Context

The package version lives in `pyproject.toml` and releases were manual. We need repeatable semver,
changelogs, GitHub Releases, and tags without editing version files by hand on every merge to `main`.

## Decision

Adopt [Release Please](https://github.com/googleapis/release-please) via
`googleapis/release-please-action@v4` on pushes to `main` (and `workflow_dispatch`).

- Manifest mode: `release-please-config.json` + `.release-please-manifest.json` (root package, Python release type).
- Release Please opens/updates a release PR; merging it bumps `pyproject.toml`, updates `CHANGELOG.md`, tags, and publishes the GitHub Release.
- Pre-1.0 semver: `bump-minor-pre-major` and `bump-patch-for-minor-pre-major` enabled for `0.x` versions.

## Consequences

- **Positive:** Conventional commits on `main` drive changelog and version bumps; CI stays separate from `data-refresh` commits.
- **Negative:** `data:` and other non-conventional commit prefixes do not appear in release notes until authors adopt `feat:` / `fix:` / `chore:` (or similar).
- **Operational:** First run on `main` may open a release PR for the next version after `0.2.0`; merge when ready.
