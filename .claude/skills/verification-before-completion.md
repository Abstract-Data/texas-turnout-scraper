---
skill: power-skills-bundle:verification-before-completion
invoke: Skill("power-skills-bundle:verification-before-completion")
---
# verification-before-completion

Use **before claiming any task is complete** — runs verification commands and confirms output.
Evidence before assertions.

Runs: pytest tests/unit -q, ruff check, ruff format --check.

Trigger phrases: "verify before done", "check before claiming complete", "is it actually done"
