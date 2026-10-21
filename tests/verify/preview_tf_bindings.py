"""Parse preview Terraform election D1 binding names for verify tests and smoke."""

from __future__ import annotations

import re
from pathlib import Path

PREVIEW_DIR = Path(__file__).resolve().parents[2] / "infra" / "preview"
MAIN_TF = PREVIEW_DIR / "main.tf"
ELECTION_D1_TF = PREVIEW_DIR / "election_d1.tf"
VARIABLES_TF = PREVIEW_DIR / "variables.tf"


def preview_tf_text() -> str:
    parts = [MAIN_TF.read_text(encoding="utf-8")]
    if ELECTION_D1_TF.is_file():
        parts.append(ELECTION_D1_TF.read_text(encoding="utf-8"))
    return "\n".join(parts)


def default_preview_source_election_id() -> str:
    text = VARIABLES_TF.read_text(encoding="utf-8")
    match = re.search(
        r'variable "preview_source_election_id"[\s\S]*?default\s*=\s*"([^"]+)"',
        text,
    )
    if match is None:
        raise AssertionError("preview_source_election_id default missing from variables.tf")
    return match.group(1)


def preview_election_d1_binding_name() -> str:
    text = preview_tf_text()
    interpolated = re.search(
        r'preview_election_d1_binding\s*=\s*"EV_\$\{var\.preview_source_election_id\}"',
        text,
    )
    if interpolated is not None:
        return f"EV_{default_preview_source_election_id()}"
    match = re.search(r'preview_election_d1_binding\s*=\s*"([^"]+)"', text)
    if match is None:
        raise AssertionError("local.preview_election_d1_binding missing from preview Terraform")
    return match.group(1)


def worker_d1_binding_names() -> list[str]:
    text = preview_tf_text()
    election_binding = preview_election_d1_binding_name()
    names: list[str] = []
    for block in re.finditer(r"\{[^{}]*type\s*=\s*\"d1\"[^{}]*\}", text):
        chunk = block.group(0)
        if re.search(r'name\s*=\s*"DB"', chunk):
            names.append("DB")
        elif "local.preview_election_d1_binding" in chunk:
            names.append(election_binding)
    return names
