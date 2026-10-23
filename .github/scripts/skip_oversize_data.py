"""Keep files over GitHub's push limit out of the data-refresh commit.

GitHub rejects any pushed file over 100 MiB. A single oversize statewide roster
used to fail the whole ``data/elections`` commit, so no election published.

When a file is over the limit, the whole election is held back so the published
data stays consistent: the election directory (or, outside the ``<source>/<id>``
layout, just that file) goes back to its committed state (or is removed if it was
never committed) and its ``index.json`` entry is restored (or
removed). Otherwise the new audit counts, report files, and index entry would
describe a roster that Pages does not serve. Other elections still publish. A
``::warning::`` names each held-back election.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

# Below the 100 MiB hard limit, leaving headroom for growth between runs.
DEFAULT_LIMIT_BYTES = 95 * 1024 * 1024

_SOURCES = ("civix", "legacy")


def _git(
    repo: Path, *args: str, check: bool = True, literal: bool = True
) -> subprocess.CompletedProcess[str]:
    """Run git. Paths are literal (no glob magic) except for check-ignore, which rejects it."""
    prefix = ["git", "--literal-pathspecs"] if literal else ["git"]
    return subprocess.run(
        [*prefix, "-C", str(repo), *args],
        check=check,
        capture_output=True,
        text=True,
    )


def _hold_back_unit(root: Path, relative: Path) -> Path:
    """The election folder ``<root>/<source>/<id>`` for a file below it, else the file."""
    parts = relative.relative_to(root).parts
    if len(parts) >= 3 and parts[0] in _SOURCES:
        return root / parts[0] / parts[1]
    return relative


def _restore_unit(repo: Path, directory: Path) -> str:
    if _git(repo, "ls-files", "--", str(directory)).stdout.strip():
        _git(repo, "checkout", "HEAD", "--", str(directory))
        _git(repo, "clean", "-fdq", "--", str(directory))
        return "restored to its last published state"
    _git(repo, "clean", "-fdq", "--", str(directory))
    return "not published"


def _restore_index_entry(repo: Path, root: Path, directory: Path) -> None:
    """Put one election's ``index.json`` entry back to HEAD's version (or drop it)."""
    source, election_id = directory.parent.name, directory.name
    if directory.parent.parent != root or source not in _SOURCES:
        return
    index_path = repo / root / "index.json"
    if not index_path.is_file():
        return
    current = json.loads(index_path.read_text())
    shown = _git(repo, "show", f"HEAD:{root / 'index.json'}", check=False)
    head = json.loads(shown.stdout) if shown.returncode == 0 else {}
    head_entry = next(
        (
            e
            for e in head.get(source, {}).get("elections", [])
            if str(e.get("source_election_id")) == election_id
        ),
        None,
    )
    entries = current.get(source, {}).get("elections", [])
    restored: list[object] = []
    placed = False
    for entry in entries:
        if str(entry.get("source_election_id")) != election_id:
            restored.append(entry)
        elif head_entry is not None and not placed:
            restored.append(head_entry)
            placed = True
    current.setdefault(source, {})["elections"] = restored
    if shown.returncode == 0 and all(current.get(k) == head.get(k) for k in _SOURCES):
        # Back to the published sections: keep HEAD's bytes, including last_updated, so
        # a held-back election does not cause a timestamp-only commit every run.
        index_path.write_text(shown.stdout)
        return
    index_path.write_text(json.dumps(current, indent=2) + "\n")


def skip_oversize(repo: Path, root: Path, limit_bytes: int) -> list[Path]:
    """Hold back every election that has a file over ``limit_bytes``."""
    skipped: list[Path] = []
    base = repo / root
    if not base.is_dir():
        return skipped
    held: dict[Path, str] = {}
    for path in sorted(base.rglob("*")):
        if not path.is_file() or path.stat().st_size <= limit_bytes:
            continue
        relative = path.relative_to(repo)
        if (
            _git(
                repo, "check-ignore", "-q", "--", str(relative), check=False, literal=False
            ).returncode
            == 0
        ):
            continue  # git never pushes ignored files, so they cannot trip the limit
        skipped.append(relative)
        directory = _hold_back_unit(root, relative)
        size_mib = path.stat().st_size / (1024 * 1024)
        if directory not in held:
            held[directory] = ""
        sys.stdout.write(
            f"::warning::{relative} is {size_mib:.1f} MiB, over the "
            f"{limit_bytes / (1024 * 1024):.0f} MiB push limit\n"
        )
    for directory in held:
        outcome = _restore_unit(repo, directory)
        _restore_index_entry(repo, root, directory)
        sys.stdout.write(f"::warning::{directory} held back: {outcome}\n")
    return skipped


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    root = Path(args[0]) if args else Path("data/elections")
    skip_oversize(Path.cwd(), root, DEFAULT_LIMIT_BYTES)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
