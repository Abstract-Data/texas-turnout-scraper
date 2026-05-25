# ADR-002: Pydantic v2 models, no SQLModel

**Date:** 2026-05-24
**Status:** Accepted

## Context
The old code used `election_utils` (a local package) for shared models. We want this
package to be self-contained and database-agnostic so any consumer can plug the output
into their own persistence layer.

## Decision
All models are Pydantic v2 BaseModel. No SQLModel, no SQLAlchemy, no ORM coupling.
The `election_utils` dependency is removed entirely.

## Consequences
- Package is database-agnostic — consumers own persistence
- No migration tooling included
- Models must be serializable to dict/JSON for GitHub Pages static API
