"""CLI entry point — mounts subcommand modules."""

from __future__ import annotations

# Side-effect imports register subcommands on the Typer apps.
from . import audit as _audit  # noqa: F401
from . import civix as _civix  # noqa: F401
from . import legacy as _legacy  # noqa: F401
from . import voterfile as _voterfile  # noqa: F401
from ._typer_apps import app


def main() -> None:
    app()


if __name__ == "__main__":
    main()
