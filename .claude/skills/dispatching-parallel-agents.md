---
skill: power-skills-bundle:dispatching-parallel-agents
invoke: Skill("power-skills-bundle:dispatching-parallel-agents")
---
# dispatching-parallel-agents

Use when you have **2+ independent tasks** with no shared state or sequential dependencies.

Example for this project: build `models.py` + `enums.py` + fixtures in parallel since they
don't depend on each other.

Trigger phrases: "do these in parallel", "run concurrently", "independent tasks"
