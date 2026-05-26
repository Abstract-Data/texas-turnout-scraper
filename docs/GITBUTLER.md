# GitButler — Virtual Branch Reference

This project uses [GitButler](https://gitbutler.com/) for branch management. GitButler tracks
multiple work-in-progress branches simultaneously without forcing `git checkout` switches, which
fits the way this project's work is sliced into parallel prompts (test fixtures + civix tests +
legacy tests run as independent tranches).

## TL;DR

| You want to... | Run |
|---|---|
| Create a new virtual branch | `gb branch create feature/{name}` |
| List active virtuals | `gb branch list` |
| Apply (switch to) a virtual | `gb branch apply feature/{name}` |
| Push a virtual to remote | `gb branch push feature/{name}` |
| Drop an unapplied virtual | `gb branch drop feature/{name}` |
| See uncommitted changes by virtual | `gb status` |

Read-only `git` commands (`git status`, `git log`, `git diff`, `git show`, `git blame`) work
normally and are not affected.

## What's blocked

The `.claude/hooks/block-raw-git.sh` PreToolUse hook blocks these subcommands when invoked
through the Claude Code Bash tool:

- `git checkout -b <name>` — creates a real branch that GitButler can't track
- `git branch ...` — same problem
- `git merge ...` — bypasses GitButler's stacking and conflict resolution

If you need to run one of these directly (e.g. annotated release tags, manual upstream sync),
set `GITBUTLER_BYPASS=1` in the shell environment for that one call.

## Common patterns

### Parallel feature work (the reason GitButler is useful here)

The prompt-driven workflow regularly has 3–4 independent tranches in flight at once
(e.g. `01-test-fixtures`, `02-unit-tests-civix`, `03-unit-tests-legacy` run as parallel
subagents). Each tranche becomes its own virtual branch:

```bash
gb branch create feature/01-test-fixtures
gb branch create feature/02-unit-tests-civix
gb branch create feature/03-unit-tests-legacy
# Work happens in the working tree; GitButler tracks per-virtual diffs automatically.
gb branch push feature/01-test-fixtures   # ready for review independently
```

### Stacking dependent changes

If `02-unit-tests-civix` depends on `01-test-fixtures`, create as a stack:

```bash
gb branch create --parent feature/01-test-fixtures feature/02-unit-tests-civix
```

GitButler will rebase the child when the parent updates.

### Conflict resolution

When two virtuals touch the same lines, GitButler highlights the conflict per-branch.
Resolve in the editor; `gb status` shows the remaining conflicts per virtual.

## Why not raw `git checkout -b`?

The block hook isn't pedantry — it's because raw branches create silent drift:

1. The branch shows in `git branch -a` but not `gb branch list`.
2. Changes made on it won't propagate to active virtuals.
3. `gb` operations that assume virtual tracking will produce inconsistent state.

If you find yourself fighting the hook, that's usually a sign the operation belongs in a
virtual branch anyway.

## Further reading

- [GitButler docs](https://docs.gitbutler.com/)
- [Virtual branches concept](https://docs.gitbutler.com/features/virtual-branches/branch-lanes)
- [Stacking branches](https://docs.gitbutler.com/features/stacked-branches/dependent-branches)
- Hook source: `.claude/hooks/block-raw-git.sh`
- ADR: `docs/adr/006-gitbutler-virtual-branches.md` (rationale for adoption)
