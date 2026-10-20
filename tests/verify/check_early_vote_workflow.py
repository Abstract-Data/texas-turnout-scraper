"""Structural checks for the early-vote sync workflow. Run with pytest tests/verify/."""

from pathlib import Path

WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "early-vote-sync.yml"


def test_workflow_run_scripts_do_not_interpolate_inputs() -> None:
    lines = WORKFLOW.read_text().splitlines()
    in_run = False
    run_blocks = 0
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("run:"):
            in_run = True
            run_blocks += 1
            assert "${{" not in line
            continue
        if in_run:
            if line.startswith("          ") or stripped == "":
                assert "${{" not in line
                continue
            in_run = False
    text = "\n".join(lines)
    assert run_blocks >= 4
    assert "group: early-vote-sync-${{ inputs.election_id }}" in text
    assert "cancel-in-progress: false" in text
    assert text.count("election_id must be digits only") == 2
    assert text.count("election_date must be MM/DD/YYYY") == 4
    assert "secrets." not in text.split("jobs:", 1)[1].split("apply:", 1)[0]
