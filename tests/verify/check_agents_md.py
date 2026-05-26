"""
Structural verification: check AGENTS.md required sections are present.
Run with: pytest tests/verify/ -q
"""

from pathlib import Path

AGENTS_MD = Path(__file__).parent.parent.parent / "AGENTS.md"

REQUIRED_SECTIONS = [
    "## Project Purpose",
    "## Repository Layout",
    "## Core Commands",
    "## Key Constraints",
    "## Documentation Priority",
    "## Goal Proposal Protocol",
    "## Session Management",
    "## Notion References",
    "## NEVER DO",
]


def test_agents_md_exists():
    assert AGENTS_MD.exists(), "AGENTS.md is missing from repo root"


def test_agents_md_not_empty():
    content = AGENTS_MD.read_text()
    assert len(content) > 500, "AGENTS.md appears to be a stub — too short"


def test_agents_md_required_sections():
    content = AGENTS_MD.read_text()
    missing = [s for s in REQUIRED_SECTIONS if s not in content]
    assert not missing, f"AGENTS.md missing required sections: {missing}"


def test_claude_md_symlink():
    claude_md = AGENTS_MD.parent / "CLAUDE.md"
    assert claude_md.exists(), "CLAUDE.md is missing"
    assert claude_md.is_symlink(), "CLAUDE.md must be a symlink to AGENTS.md"
    assert claude_md.resolve() == AGENTS_MD.resolve(), (
        "CLAUDE.md symlink does not resolve to AGENTS.md"
    )
