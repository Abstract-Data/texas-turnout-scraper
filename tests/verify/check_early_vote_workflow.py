"""Structural checks for the early-vote sync workflow. Run with pytest tests/verify/."""

import os
import re
import subprocess
from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"
WORKFLOW = WORKFLOWS / "early-vote-sync.yml"


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
    group_line = (
        "group: early-vote-sync-${{ github.event_name == 'schedule' "
        "&& '53815' || inputs.election_id }}"
    )
    assert group_line in text
    assert "17 15,16,22,23,3,4 * * *" in text
    assert "sync_mode" in text
    assert "cancel-in-progress: false" in text
    assert text.count("election_id must be digits only") == 2
    assert text.count("election_date is required when sync_mode=single") == 2
    assert text.count("election_date must be MM/DD/YYYY") == 2
    assert "secrets." not in text.split("jobs:", 1)[1].split("apply:", 1)[0]


def _run_scripts(text: str) -> list[str]:
    scripts: list[str] = []
    current: list[str] | None = None
    for line in text.splitlines():
        if line.strip().startswith("run:"):
            if current is not None:
                scripts.append("\n".join(current))
            current = [line]
        elif current is not None and (line.startswith("          ") or not line.strip()):
            current.append(line)
        elif current is not None:
            scripts.append("\n".join(current))
            current = None
    if current is not None:
        scripts.append("\n".join(current))
    return scripts


def test_all_third_party_actions_are_sha_pinned() -> None:
    pinned = re.compile(r"uses: [\w.-]+/[\w./-]+@[0-9a-f]{40} # v\d")
    for path in sorted(WORKFLOWS.glob("*.yml")):
        for line in path.read_text().splitlines():
            if "uses:" in line and not line.strip().startswith("#"):
                assert pinned.search(line), f"{path.name}: unpinned action: {line.strip()}"


def test_uv_sync_is_locked_or_frozen() -> None:
    for path in sorted(WORKFLOWS.glob("*.yml")):
        for line in path.read_text().splitlines():
            if "uv sync" in line and not line.strip().startswith("#"):
                assert "--locked" in line or "--frozen" in line, f"{path.name}: {line.strip()}"


def test_ci_workflows_declare_read_only_permissions() -> None:
    for name in ("ci.yml", "tool-config-verify.yml"):
        text = (WORKFLOWS / name).read_text()
        assert re.search(r"^permissions:\n  contents: read$", text, re.M), name


def test_data_refresh_run_scripts_do_not_interpolate_expressions() -> None:
    text = (WORKFLOWS / "data-refresh.yml").read_text()
    for script in _run_scripts(text):
        assert "${{" not in script, script
    assert "election_id must be digits only" in text


def test_terraform_apply_passes_plan_sha_via_env() -> None:
    text = (WORKFLOWS / "terraform-preview.yml").read_text()
    for script in _run_scripts(text):
        assert "needs.plan.outputs" not in script
    assert "PLAN_SHA256: ${{ needs.plan.outputs.plan_sha256 }}" in text


def test_terraform_drift_gate_exempts_only_reproducible_dist_info_files() -> None:
    text = (WORKFLOWS / "terraform-preview.yml").read_text()
    script = (
        WORKFLOWS.parents[1] / "infra/preview/worker/scripts/filter_pywrangler_drift.py"
    ).read_text()
    assert "filter_pywrangler_drift.py" in text
    assert "(RECORD|direct_url[.]json|uv_cache[.]json)$" in script
    assert "dist-info/' ||" not in text


def test_scheduled_runs_refetch_every_day_not_just_the_latest() -> None:
    """Portal day files are daily and can gain late records.

    So each scheduled run re-hashes every day.

    backfill is hash-gated per day: an unchanged day writes nothing, a changed day is rewritten
    and the KV version bumps. `latest` would only ever look at today's file.
    """
    text = WORKFLOW.read_text()
    assert text.count("github.event_name == 'schedule' && 'backfill' || inputs.sync_mode") == 4
    assert "github.event_name == 'schedule' && 'latest'" not in text


def test_apply_step_names_the_snapshot_bucket() -> None:
    """The S3 keys come from 1Password; the bucket name is not secret and lives here."""
    text = WORKFLOW.read_text()
    assert text.count("EV_SNAPSHOT_BUCKET: tx-turnout-ev-snapshots-preview") == 1


def test_tflint_plugin_download_is_authenticated() -> None:
    """Unauthenticated `tflint --init` hits the GitHub API rate limit and fails the PR."""
    for workflow in WORKFLOWS.glob("*.yml"):
        text = workflow.read_text()
        for index in [i for i in range(len(text)) if text.startswith("tflint --init", i)]:
            window = text[max(0, index - 300) : index]
            assert "GITHUB_TOKEN: ${{ github.token }}" in window, workflow.name


def test_roster_rows_backfill_runs_after_the_aggregate_backfill() -> None:
    """Days stored before the row table existed get rows from this idempotent step."""
    text = WORKFLOW.read_text()
    aggregates = text.index("early_vote_d1_precompute_backfill")
    rows = text.index("early_vote_roster_rows_backfill")
    assert aggregates < rows
    assert text.count("early_vote_roster_rows_backfill") == 1


def test_geography_load_runs_once_after_the_roster_rows_backfill() -> None:
    """Aliases resolve the live roster pairs, so the rows must be loaded first."""
    text = WORKFLOW.read_text()
    assert text.count("Load geography and precinct aliases (idempotent)") == 1
    assert text.count("texas_turnout_scraper.geo_load") == 1
    assert text.index("early_vote_roster_rows_backfill") < text.index("geo_load")
    step = text.split("Load geography and precinct aliases (idempotent)", 1)[1]
    assert "op run --environment s6tljdcafkgqxfuxhcnj3m5x5a" in step
    assert '--election-id "${ELECTION_ID}" --apply' in step


def test_repeat_build_runs_once_after_the_geography_load() -> None:
    """The repeat flags read the live roster rows, so they run after every roster step."""
    text = WORKFLOW.read_text()
    name = "Build repeat VUID flags (idempotent)"
    assert text.count(name) == 1
    assert text.count("texas_turnout_scraper.repeat_build") == 1
    assert text.index("Load geography and precinct aliases (idempotent)") < text.index(name)
    step = text.split(name, 1)[1]
    assert "op run --environment s6tljdcafkgqxfuxhcnj3m5x5a" in step
    assert '--election-id "${ELECTION_ID}" --apply' in step
    secrets = step.split("run: |", 1)[0]
    assert "OP_SERVICE_ACCOUNT_TOKEN: ${{ secrets.OP_SERVICE_ACCOUNT_TOKEN }}" in secrets


def test_loader_jobs_do_not_install_the_geo_extra() -> None:
    """The geo libraries are for the offline build only; the loader reads the committed JSON."""
    assert "--extra geo" not in WORKFLOW.read_text()


def test_loader_jobs_install_the_loader_extra() -> None:
    """The roster-row loader imports sqlmodel, which only the loader extra installs."""
    syncs = [line for line in WORKFLOW.read_text().splitlines() if "uv sync" in line]
    assert len(syncs) == 2
    assert all("--extra waf --extra loader" in line for line in syncs)


def _window_script() -> str:
    text = WORKFLOW.read_text()
    block = text.split("  window:", 1)[1].split("\n  dry-run:", 1)[0]
    script = block.split("run: |\n", 1)[1]
    return "\n".join(line[10:] for line in script.splitlines())


def _window_output(tmp_path: Path, event: str, now_utc: str, chicago_hour: str) -> str:
    """Run the window script with a fake ``date`` so the clock is an input."""
    shim = tmp_path / "date"
    shim.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys, calendar, time\n"
        "a = sys.argv[1:]\n"
        "if a == ['+%H']:\n"
        "    print(os.environ['FAKE_HOUR'])\n"
        "elif a[:2] == ['-u', '-d']:\n"
        "    print(calendar.timegm(time.strptime(a[2], '%Y-%m-%d %H:%M:%S')))\n"
        "elif a == ['-u', '+%s']:\n"
        "    print(calendar.timegm(time.strptime(os.environ['FAKE_NOW'], '%Y-%m-%d %H:%M:%S')))\n"
        "else:\n"
        "    sys.exit(2)\n"
    )
    shim.chmod(0o755)
    out = tmp_path / "out"
    out.write_text("")
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "EVENT_NAME": event,
        "FAKE_NOW": now_utc,
        "FAKE_HOUR": chicago_hour,
        "GITHUB_OUTPUT": str(out),
    }
    subprocess.run(["bash", "-c", _window_script()], check=True, env=env, capture_output=True)
    return out.read_text().strip()


def test_schedule_runs_three_times_a_day_until_the_end_of_november_3(tmp_path: Path) -> None:
    text = WORKFLOW.read_text()
    assert 'cron: "17 15,16,22,23,3,4 * * *"' in text
    assert "needs: window" in text
    assert "needs.window.outputs.proceed == 'true'" in text
    kept = ["10", "17", "22"]
    for hour in kept:
        assert _window_output(tmp_path, "schedule", "2026-10-30 15:20:00", hour) == "proceed=true"
    for hour in ("09", "11", "16", "18", "21", "23", "04"):
        assert _window_output(tmp_path, "schedule", "2026-10-30 15:20:00", hour) == "proceed=false"
    assert _window_output(tmp_path, "schedule", "2026-11-04 04:20:00", "22") == "proceed=true"
    assert _window_output(tmp_path, "schedule", "2026-11-04 06:00:00", "10") == "proceed=false"
    assert (
        _window_output(tmp_path, "workflow_dispatch", "2026-12-01 00:00:00", "03") == "proceed=true"
    )
