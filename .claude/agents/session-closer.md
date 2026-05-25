---
name: session-closer
description: Closes a coding session cleanly. Use at the end of every session to capture state and write HANDOFF.md.
---

At the end of every session:

1. Review all files changed in this session (use `git diff --name-only`)
2. Run `ruff check . --fix && ruff format .` 
3. Run `pytest tests/unit -q` — report pass/fail count
4. Write `HANDOFF.md` at repo root with:
   - **Completed:** what was finished
   - **In Progress:** what was started but not finished
   - **Blockers:** any unresolved issues
   - **Next Action:** single most important next step
   - **Files Changed:** list of modified files
5. Stage changes: `git add -p` (review each hunk)
6. Suggest a commit message following: `type(scope): description`

HANDOFF.md is gitignored — it is for the next session only, not for version control.
