"""terraform-preview.yml must not fail just because a release renamed the package's dist-info."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "infra" / "preview" / "worker" / "scripts" / "filter_pywrangler_drift.py"
WORKFLOW = ROOT / ".github" / "workflows" / "terraform-preview.yml"
PM = "infra/preview/worker/python_modules"


def _filter(lines: list[str]) -> list[str]:
    spec = importlib.util.spec_from_file_location("filter_pywrangler_drift", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.unexpected_drift(lines)


def test_a_release_bump_is_not_drift() -> None:
    """The measured file set after bumping 0.2.7 to 0.2.8 and running pywrangler sync."""
    names = [
        "INSTALLER", "METADATA", "RECORD", "REQUESTED", "WHEEL",
        "direct_url.json", "entry_points.txt", "uv_build.json", "uv_cache.json",
    ]  # fmt: skip
    lines = [f" D {PM}/texas_turnout_scraper-0.2.7.dist-info/{n}" for n in names]
    lines += [f"?? {PM}/texas_turnout_scraper-0.2.8.dist-info/"]
    lines += [f" M {PM}/texas_turnout_scraper/__init__.py"]
    lines += [f" M {PM}/fastapi-0.156.1.dist-info/RECORD"]
    assert _filter(lines) == []


def test_real_drift_is_still_reported() -> None:
    changed = f" M {PM}/texas_turnout_scraper/early_vote_sync.py"
    dep = f" M {PM}/httpx-0.28.1.dist-info/METADATA"
    pylock = " M infra/preview/worker/pylock.toml"
    assert _filter([changed, dep, pylock]) == [changed, dep, pylock]


def test_workflow_uses_the_filter_script() -> None:
    text = WORKFLOW.read_text()
    assert "scripts/filter_pywrangler_drift.py" in text
    assert "dist-info/(RECORD|direct_url" not in text
