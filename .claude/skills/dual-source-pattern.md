# Skill: dual-source-pattern

Use when:
- Adding a third roster source
- Tempted to copy a function from `civix.py` into a new module
- Writing the second variant of a CLI command and the first one already exists
- Adding a new `finding_type` to the audit pipeline
- Adding a new domain enum value (`Source`, `VoteMethod`, `ElectionType`)

This is a thin pointer to the full playbook. The substance lives in:

→ **`docs/playbooks/dual-source-pattern.md`** — heuristic table for share-vs-split, the
  `RosterSource` Protocol signature, audit vocabulary rules, the PR-review smell checklist.

→ **`docs/adr/008-rostersource-protocol.md`** — the architectural decision and rationale.

→ **`prompts/10-review-remediation/`** — Phase 4 of the active remediation implements the
  Protocol; read it before designing a parallel implementation.

→ **`tests/unit/test_audit_contract.py`** — the vocabulary guard. Add to this if you're
  introducing a new `finding_type`.

→ **`.claude/agents/architecture-guardian.md`** — invoke this subagent before merging any PR
  that adds a new module, CLI subcommand, or MCP tool.

## Quick decision tree

```
Are you adding a function that does roughly the same thing as one in civix.py or legacy_api.py?
├── YES → STOP. Read docs/playbooks/dual-source-pattern.md. Consider the RosterSource protocol.
└── NO → continue.

Are you introducing a new domain string literal (source, finding_type, voting_method)?
├── YES → STOP. Add it to the canonical enum in enums.py. Update test_audit_contract.py if it's
│         a finding_type.
└── NO → continue.

Are you about to reach into another module's _private attribute?
├── YES → STOP. ruff SLF will block this. Promote the attribute to public, or add a context
│         manager (e.g. session.with_pace(...)).
└── NO → carry on.
```
