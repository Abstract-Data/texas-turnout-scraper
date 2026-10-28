"""The Worker cron trigger in Terraform fires on the hours the handler expects."""

from __future__ import annotations

import re
from pathlib import Path

from ev_api.cron import CRON_SCHEDULE

ROOT = Path(__file__).resolve().parents[2]
PREVIEW = ROOT / "infra" / "preview"
TERRAFORM_WORKFLOW = ROOT / ".github" / "workflows" / "terraform-preview.yml"


def _block(text: str, header: str) -> str:
    start = text.index(header)
    depth = 0
    for index in range(text.index("{", start), len(text)):
        depth += {"{": 1, "}": -1}.get(text[index], 0)
        if depth == 0:
            return text[start : index + 1]
    raise AssertionError(f"unterminated block {header}")


def test_terraform_cron_matches_the_handler_schedule() -> None:
    ev_sync = (PREVIEW / "ev_sync.tf").read_text()
    trigger = _block(ev_sync, 'resource "cloudflare_workers_cron_trigger" "ev_sync"')
    assert re.findall(r'cron\s*=\s*"([^"]+)"', trigger) == [CRON_SCHEDULE]
    assert "script_name = cloudflare_worker.ev_sync.name" in trigger


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
