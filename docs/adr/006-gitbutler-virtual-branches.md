# ADR 006: GitButler virtual branches for parallel feature work

**Date:** 2026-05-25
**Status:** accepted

## Context

The prompt-driven workflow in this repo regularly has 3–4 independent tranches in flight at
once — for example, the 10-prompt refactor plan runs `01-test-fixtures`, `02-unit-tests-civix`,
and `03-unit-tests-legacy` as parallel subagent jobs. Each tranche touches a different set of
files, but they share the same working tree.

Native `git` requires committing or stashing every time you switch context. That cadence breaks
agent workflows that interleave read/write across multiple branches in a single session:
intermediate stashes pile up, branch state diverges from agent expectations, and the cost of a
"quick switch to check something" is high enough that agents avoid it (producing larger,
harder-to-review commits).

GitButler tracks multiple branches as *virtual* lanes against a single working tree, so an
agent can write to file X on branch A and file Y on branch B in the same session without ever
calling `git checkout`.

## Decision

Use [GitButler](https://gitbutler.com/) for all feature-branch operations. Block raw
`git checkout -b`, `git branch`, and `git merge` via the `.claude/hooks/block-raw-git.sh`
PreToolUse hook.

Read-only `git` commands (`status`, `diff`, `log`, `show`, `blame`) remain unaffected — they
work the same way regardless of GitButler.

## Consequences

**Easier:**
- Parallel agent work without intermediate stashes.
- Per-virtual diffs always reflect only that virtual's intended changes.
- Stacking dependent virtuals via `gb branch create --parent` mirrors the prompt-dependency DAG.

**Harder:**
- Onboarding cost: a new contributor has to learn `gb` commands. Mitigated by `docs/GITBUTLER.md`
  and a hook error message that points there.
- Some `git` workflows (release tagging, manual upstream sync) need `GITBUTLER_BYPASS=1`.
- CI runs against real branches, so the merge train still uses `git merge` at the end —
  GitButler's job is local-only.

**Trade-offs considered:**
- *Worktrees:* `git worktree` solves the same parallelism problem but creates separate working
  directories. That's incompatible with the single-IDE / single-shell agent loop here.
- *Stacking with `git rebase --interactive`:* works but adds rebase-conflict cost to every
  context switch.
- *Stay on native git:* would force the agent loop into a "commit-everything-before-switching"
  pattern that produces unreviewable WIP commits.

## References

- `docs/GITBUTLER.md` — virtual-branch reference for this project
- `.claude/hooks/block-raw-git.sh` — the hook that enforces the decision
- AGENTS.md → `## GitButler` section
