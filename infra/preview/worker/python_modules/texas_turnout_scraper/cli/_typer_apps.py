"""Typer app instances and shared CLI constants."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

app = typer.Typer(name="tx-turnout", help="Texas SOS early-voting data tool.")
civix_app = typer.Typer(help="Commands for the Civix EVR portal (2025+)")
legacy_app = typer.Typer(help="Commands for the legacy SOS portal (pre-2025)")
audit_app = typer.Typer(help="Data quality audit commands")
voterfile_app = typer.Typer(
    help="Voterfile matching — join a statewide voterfile against an EV roster"
)

app.add_typer(civix_app, name="civix")
app.add_typer(legacy_app, name="legacy")
app.add_typer(audit_app, name="audit")
app.add_typer(voterfile_app, name="voterfile")

_DEFAULT_INDEX_PATH = Path("data/elections/index.json")
_FRESHNESS_HOURS = 24

EvDateStr = Annotated[str, typer.Argument(help="EV date in YYYY-MM-DD format")]
