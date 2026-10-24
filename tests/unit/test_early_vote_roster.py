"""Roster canonicalization before hashing and D1: every column kept, bytes stable."""

from __future__ import annotations

import ast
import csv
import io
from pathlib import Path

import pytest

from texas_turnout_scraper.early_vote_roster import canonicalize_roster_csv, summarize_roster_csv
from texas_turnout_scraper.early_vote_sync import prepare_payload_bytes, sha256_decoded

FIXTURE_CSV = (
    Path(__file__).parent.parent / "fixtures" / "early_vote" / "statewide_roster_sample.csv"
)

PORTAL_FIVE_COLUMN = (
    b'"COUNTY","VOTER_NAME","ID_VOTER","VOTING_METHOD","PRECINCT"\r\n'
    b'"TRAVIS","SMITH, ALICE A","1000000001","IN-PERSON","100"\r\n'
    b'"BEXAR","O""NEIL, PAT\nJR","0000000002","MAIL-IN",""\r\n'
    b'"HARRIS","NUNEZ MARIA","0000000003","MAIL-IN","200"\r\n'
)


def _old_redact_oracle(decoded: bytes) -> bytes:
    """The removed allowlist redaction, kept verbatim as the byte oracle for five-column files."""
    allow = {"COUNTY", "VOTER_NAME", "ID_VOTER", "VOTING_METHOD", "PRECINCT"}
    reader = csv.DictReader(io.StringIO(decoded.decode("utf-8")))
    assert reader.fieldnames is not None
    kept = [h for h in reader.fieldnames if h in allow]
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=kept, lineterminator="\n")
    writer.writeheader()
    for row in reader:
        writer.writerow({key: row.get(key, "") for key in kept})
    return out.getvalue().encode("utf-8")


def test_a_five_column_portal_file_canonicalizes_to_the_old_bytes_so_hashes_hold() -> None:
    canonical, _ = canonicalize_roster_csv(PORTAL_FIVE_COLUMN)
    assert canonical == _old_redact_oracle(PORTAL_FIVE_COLUMN)
    assert canonical == (
        b"COUNTY,VOTER_NAME,ID_VOTER,VOTING_METHOD,PRECINCT\n"
        b'TRAVIS,"SMITH, ALICE A",1000000001,IN-PERSON,100\n'
        b'BEXAR,"O""NEIL, PAT\nJR",0000000002,MAIL-IN,\n'
        b"HARRIS,NUNEZ MARIA,0000000003,MAIL-IN,200\n"
    )


def test_an_unknown_column_is_kept_with_its_values() -> None:
    canonical, summary = canonicalize_roster_csv(FIXTURE_CSV.read_bytes())
    assert summary.columns == (
        "COUNTY",
        "VOTER_NAME",
        "ID_VOTER",
        "VOTING_METHOD",
        "PRECINCT",
        "POLL_PLACE_NAME",
    )
    assert summary.est_rows == 3
    assert dict(summary.voting_method_counts) == {"IN-PERSON": 1, "MAIL-IN": 2}
    lines = canonical.decode("utf-8").splitlines()
    assert lines[0].endswith(",POLL_PLACE_NAME")
    assert lines[1].endswith(",EXAMPLE SITE A")


def test_a_bom_is_dropped_and_ragged_rows_keep_every_cell() -> None:
    canonical, summary = canonicalize_roster_csv(
        b"\xef\xbb\xbfCOUNTY,VOTER_NAME\nTRAVIS,A,EXTRA\n\nBEXAR\n"
    )
    assert canonical == b"COUNTY,VOTER_NAME\nTRAVIS,A,EXTRA\nBEXAR\n"
    assert summary.columns == ("COUNTY", "VOTER_NAME")
    assert summary.est_rows == 2


def test_an_empty_payload_canonicalizes_to_nothing() -> None:
    canonical, summary = canonicalize_roster_csv(b"")
    assert (canonical, summary.columns, summary.est_rows) == (b"", (), 0)


def test_hash_covers_canonical_bytes_not_raw_envelope() -> None:
    canonical, _ = prepare_payload_bytes(PORTAL_FIVE_COLUMN, "statewide_roster")
    assert sha256_decoded(canonical) != sha256_decoded(PORTAL_FIVE_COLUMN)


def test_summarize_reports_aggregates_only(capsys: pytest.CaptureFixture[str]) -> None:
    summary = summarize_roster_csv(FIXTURE_CSV.read_bytes())
    text = f"{summary.columns} {summary.est_rows} {summary.voting_method_counts}"
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
