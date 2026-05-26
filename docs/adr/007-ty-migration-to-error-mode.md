# ADR 007: `ty` migration to error mode (per-file ratchet)

**Date:** 2026-05-25
**Status:** accepted (ratchet plan; per-file overrides land incrementally)

## Context

`pyproject.toml` currently has:

```toml
[tool.ty.rules]
# Warn-only until CLI/MCP wiring catches up with module APIs (see ty check output).
all = "warn"
```

This was a pragmatic choice when the CLI was being decomposed and many `Any` returns and
missing annotations would have blocked CI. But warn-only has a cost: it silenced the exact
class of error that became P1-ARCH-001 in the 2026-05-25 review — `mcp_server.py` called
`CivixClient.fetch_ev_turnout(ev_date=...)` when the actual signature was
`(election_date=...)`. Strict `ty` would have flagged this as `invalid-argument`. Warn-only
let it ship.

The structural fix from that review's Strategic Initiative S1 is "switch `tool.ty.rules.all`
from `warn` to `error`". A big-bang flip would generate hundreds of warnings and effectively
block all commits. The realistic path is a per-file ratchet.

## Decision

Migrate `ty` to error mode incrementally, **one module at a time**, using
`[[tool.ty.overrides]]` blocks. Start with the modules where the cost of latent bugs is
highest:

| Order | Module | Rationale |
|---|---|---|
| 1 | `mcp_server.py` | External contract; agents call into this. P1-ARCH-001 happened here. |
| 2 | `models.py` | Pydantic shape errors propagate everywhere downstream. |
| 3 | `legacy_api.py` | CLI + MCP both depend on it. |
| 4 | `civix.py` | Public-API surface for agents. |
| 5 | `voterfile.py` | Has the `callable` mis-annotation (P2-CODE-002) caught by ty. |
| 6 | `audit.py` + `writer.py` | After Phase 3 unification — single audit pipeline first. |
| 7 | `roster.py`, `turnout.py`, `elections.py`, `session.py` | After Phase 2 encapsulation work. |
| 8 | `cli.py` | After Phase 4 decomposition (cli/ subpackage). |
| 9 | Flip `[tool.ty.rules].all = "error"` and remove per-file overrides. |

For each module:

```toml
[[tool.ty.overrides]]
src = ["src/texas_turnout_scraper/mcp_server.py"]
rules = { all = "error" }
```

When a module is added to the strict set, fix every remaining warning in that single PR — no
partial migrations. If a specific warning is intentional, add a one-line directive:

```python
return cast(Any, value)  # ty: ignore[invalid-cast]  # legacy SOS row dicts; see docs/EARLY_VOTING_ROSTER.md
```

The rationale comment is required. NEVER DO entry in AGENTS.md enforces this.

## Consequences

**Easier:**
- Each module's strict transition is a contained PR with a bounded fix list.
- The order targets the highest-leverage modules first; the most likely place for the next
  P1 bug is also the first module made strict.
- New code added to strict modules can't introduce new warnings (CI blocks them).

**Harder:**
- Two-state `pyproject.toml` for the duration of the migration (overrides + global warn).
- Each module's strict transition requires a dedicated PR; don't bundle with feature work.

**Trade-offs considered:**
- *Big-bang error mode:* would block the next ~50 commits while warnings are paid down. Not
  viable given active feature work.
- *Stay warn-only forever:* makes the P1-ARCH-001 class of bug recurring. Not acceptable
  post-review.
- *Pylint/mypy instead of ty:* the project chose `ty` deliberately for speed and Pydantic v2
  awareness; this ADR doesn't re-litigate that.

## Tracking

Each migration step is its own commit/PR with title `ty(strict): {module_name}`. The strict
modules section of `pyproject.toml` is the canonical "what's migrated" list — no separate
issue tracker needed.

Once step 9 lands, this ADR is superseded by a one-line note in AGENTS.md ("strict ty is on for
the whole project") and the ADR is marked `superseded by completion`.

## References

- 2026-05-25 Code Review Report — P1-ARCH-001 (broken MCP keyword args) is what surfaced this
- AGENTS.md → `## Anti-Pattern Warnings` (the `# type: ignore` rationale rule)
- `prompts/10-review-remediation/` — strategic initiative S1 tracks this ADR
