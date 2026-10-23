"""Keep files over GitHub's push limit out of the data-refresh commit.

GitHub rejects any pushed file over 100 MiB. A single oversize statewide roster
used to fail the whole ``data/elections`` commit, so no election published. This
step removes such a file from the working tree (or restores its tracked version)
and prints a ``::warning::`` naming it, so everything else still publishes.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

# Below the 100 MiB hard limit, leaving headroom for growth between runs.
DEFAULT_LIMIT_BYTES = 95 * 1024 * 1024


def _is_tracked(repo: Path, relative: Path) -> bool:
    result = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "--error-unmatch", "--", str(relative)],
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def skip_oversize(repo: Path, root: Path, limit_bytes: int) -> list[Path]:
    """Drop or restore every file under ``root`` larger than ``limit_bytes``."""
    skipped: list[Path] = []
    base = repo / root
    if not base.is_dir():
        return skipped
    for path in sorted(base.rglob("*")):
        if not path.is_file() or path.stat().st_size <= limit_bytes:
            continue
        relative = path.relative_to(repo)
        size_mib = path.stat().st_size / (1024 * 1024)
        if _is_tracked(repo, relative):
            subprocess.run(
                ["git", "-C", str(repo), "checkout", "HEAD", "--", str(relative)],
                check=True,
                capture_output=True,
            )
            action = "kept the previously published version"
        else:
            path.unlink()
            action = "not published"
        sys.stdout.write(
            f"::warning::{relative} is {size_mib:.1f} MiB, over the "
            f"{limit_bytes / (1024 * 1024):.0f} MiB push limit; {action}\n"
        )
        skipped.append(relative)
    return skipped


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    root = Path(args[0]) if args else Path("data/elections")
    skip_oversize(Path.cwd(), root, DEFAULT_LIMIT_BYTES)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
