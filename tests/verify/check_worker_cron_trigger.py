"""The early-vote cron fires on the hours the gate expects, from one Worker only (ADR-017)."""

from __future__ import annotations

import json
import re
from pathlib import Path

from ev_api.cron import CRON_SCHEDULE

ROOT = Path(__file__).resolve().parents[2]
PREVIEW = ROOT / "infra" / "preview"
TERRAFORM_WORKFLOW = ROOT / ".github" / "workflows" / "terraform-preview.yml"
EV_INGEST = ROOT / "infra" / "ev_ingest"


def _block(text: str, header: str) -> str:
    start = text.index(header)
    depth = 0
    for index in range(text.index("{", start), len(text)):
        depth += {"{": 1, "}": -1}.get(text[index], 0)
        if depth == 0:
            return text[start : index + 1]
    raise AssertionError(f"unterminated block {header}")


def _wrangler_config() -> dict:
    lines = (EV_INGEST / "wrangler.jsonc").read_text().splitlines()
    return json.loads("\n".join(line for line in lines if not line.lstrip().startswith("//")))


def test_the_ingest_worker_cron_matches_the_gate_schedule() -> None:
    config = _wrangler_config()
    assert config["triggers"]["crons"] == [CRON_SCHEDULE]
    schedule_ts = (EV_INGEST / "src" / "schedule.ts").read_text()
    assert f'export const CRON_SCHEDULE = "{CRON_SCHEDULE}";' in schedule_ts


def test_the_ingest_worker_ships_with_ev_sync_mode_off() -> None:
    assert _wrangler_config()["vars"]["EV_SYNC_MODE"] == "off"


def test_no_terraform_cron_trigger_remains() -> None:
    for tf in PREVIEW.glob("*.tf"):
        assert "cloudflare_workers_cron_trigger" not in tf.read_text(), tf.name


def test_dispatch_token_variable_is_sensitive_and_bound_as_a_secret() -> None:
    variables = (PREVIEW / "variables.tf").read_text()
    block = _block(variables, 'variable "github_dispatch_token"')
    assert re.search(r"sensitive\s*=\s*true", block)
    assert re.search(r'default\s*=\s*""', block)
    main = (PREVIEW / "main.tf").read_text()
    assert re.search(
        r'type\s*=\s*"secret_text"\s*\n\s*name\s*=\s*"GITHUB_DISPATCH_TOKEN"\s*\n'
        r"\s*text\s*=\s*var\.github_dispatch_token",
        main,
    )


def test_terraform_plan_reads_the_token_from_an_env_secret_only() -> None:
    text = TERRAFORM_WORKFLOW.read_text()
    line = "TF_VAR_github_dispatch_token: ${{ secrets.EV_DISPATCH_GITHUB_TOKEN }}"
    assert text.count(line) == 1
    plan_step = text.split("- name: Plan and store tfplan", 1)[1].split("run: |", 1)[0]
    assert line in plan_step
