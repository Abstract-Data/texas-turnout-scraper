"""The git hook must block commits that touch Terraform unless fmt and validate pass."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "githooks" / "terraform-proof.sh"
GOOD = (
    'variable "name" {\n  type    = string\n  default = "x"\n}\n\n'
    'output "name" {\n  value = var.name\n}\n'
)
UNFORMATTED = 'variable "name" {\ntype = string\n default = "x"\n}\n'
INVALID = 'output "name" {\n  value = var.missing\n}\n'

needs_terraform = pytest.mark.skipif(shutil.which("terraform") is None, reason="terraform missing")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "infra").mkdir()
    (tmp_path / "README.md").write_text("x\n")
    (tmp_path / "infra" / "main.tf").write_text(GOOD)
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "init")
    return tmp_path


def _run(repo: Path, *args: str, path: str | None = None) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "TF_PROOF_DIRS": "infra"}
    if path is not None:
        env["PATH"] = path
    return subprocess.run(
        ["bash", str(SCRIPT), *args], cwd=repo, env=env, capture_output=True, text=True
    )


def test_no_terraform_change_passes_without_terraform(repo: Path) -> None:
    (repo / "README.md").write_text("changed\n")
    result = _run(repo, path="/usr/bin:/bin")
    assert result.returncode == 0, result.stdout + result.stderr


@needs_terraform
def test_unformatted_terraform_is_blocked(repo: Path) -> None:
    (repo / "infra" / "main.tf").write_text(UNFORMATTED)
    result = _run(repo)
    assert result.returncode != 0
    assert "terraform fmt" in result.stdout + result.stderr


@needs_terraform
def test_invalid_terraform_is_blocked(repo: Path) -> None:
    (repo / "infra" / "main.tf").write_text(INVALID)
    result = _run(repo)
    assert result.returncode != 0
    assert "terraform validate" in result.stdout + result.stderr


@needs_terraform
def test_formatted_valid_terraform_passes_and_leaves_the_tree_untouched(repo: Path) -> None:
    (repo / "infra" / "main.tf").write_text(GOOD + "\n# note\n")
    before = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True
    ).stdout
    result = _run(repo)
    after = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True
    ).stdout
    assert result.returncode == 0, result.stdout + result.stderr
    assert before == after


@needs_terraform
def test_base_ref_catches_a_bad_file_that_is_already_committed(repo: Path) -> None:
    (repo / "infra" / "main.tf").write_text(INVALID)
    _git(repo, "commit", "-qam", "bad")
    assert _run(repo).returncode == 0
    assert _run(repo, "--base", "HEAD~1").returncode != 0
