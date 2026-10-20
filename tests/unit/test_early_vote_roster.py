"""Legal guardrails for EV roster redaction before D1."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from texas_turnout_scraper.early_vote_roster import (
    COUNTY_ROSTER_COLUMNS,
    STATEWIDE_ROSTER_COLUMNS,
    redact_roster_csv,
    summarize_roster_csv,
)
from texas_turnout_scraper.early_vote_sync import prepare_payload_bytes, sha256_decoded

FIXTURE_CSV = (
    Path(__file__).parent.parent / "fixtures" / "early_vote" / "statewide_roster_sample.csv"
)


def test_redact_drops_polling_place_column_before_d1() -> None:
    raw = FIXTURE_CSV.read_bytes()
    redacted, summary = redact_roster_csv(raw, statewide=True)

    assert "POLL_PLACE_NAME" in summary.dropped_columns
    assert frozenset(summary.columns) == STATEWIDE_ROSTER_COLUMNS
    assert summary.est_rows == 3
    assert dict(summary.voting_method_counts) == {"IN-PERSON": 1, "MAIL-IN": 2}
    header = redacted.decode("utf-8").splitlines()[0]
    assert "POLL_PLACE" not in header
    assert "VOTER_NAME" in header


def test_county_roster_allowlist_excludes_county_column() -> None:
    raw = FIXTURE_CSV.read_bytes()
    _redacted, summary = redact_roster_csv(raw, statewide=False)

    assert "COUNTY" in summary.dropped_columns
    assert frozenset(summary.columns) == COUNTY_ROSTER_COLUMNS


def test_hash_covers_redacted_bytes_not_raw_envelope() -> None:
    raw = FIXTURE_CSV.read_bytes()
    redacted, _ = prepare_payload_bytes(raw, "statewide_roster")
    assert sha256_decoded(redacted) != sha256_decoded(raw)


def test_summarize_reports_aggregates_only(capsys: pytest.CaptureFixture[str]) -> None:
    summary = summarize_roster_csv(FIXTURE_CSV.read_bytes(), statewide=True)
    text = f"{summary.columns} {summary.dropped_columns} {summary.est_rows}"
    assert "SMITH" not in text
    assert "1000000001" not in text
    assert summary.est_rows == 3


def _module_import_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_evr_sync_modules_do_not_import_voterfile() -> None:
    root = Path(__file__).resolve().parents[2] / "src" / "texas_turnout_scraper"
    for name in ("early_vote_sync.py", "early_vote_roster.py"):
        imports = _module_import_names(root / name)
        assert "voterfile" not in imports
