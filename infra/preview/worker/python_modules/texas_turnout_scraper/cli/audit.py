"""Data quality audit CLI subcommands."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Annotated

import typer

from ._common import _parse_ev_date
from ._typer_apps import audit_app


@audit_app.command("run")
def audit_run(
    election_id: Annotated[str, typer.Argument(help="Election ID (source_election_id string)")],
    ev_date: Annotated[
        str | None,
        typer.Argument(
            help="Optional YYYY-MM-DD report label (default: latest date in roster CSV)",
        ),
    ] = None,
    source: Annotated[
        str, typer.Option("--source", help="Data source: 'civix' or 'legacy'")
    ] = "civix",
    data_dir: Annotated[Path, typer.Option("--data-dir", help="Root data directory")] = Path(
        "data"
    ),
    output: Annotated[
        str, typer.Option("--output", "-o", help="Output format: 'table' or 'json'")
    ] = "table",
) -> None:
    """Run data quality audit on a stored combined roster (roster_ev_{id}.csv)."""
    from ..audit import audit_records
    from ..writer import (
        load_stored_turnout_for_audit,
        read_roster_csv,
        report_date_from_roster_csv,
        stored_audit_ev_path,
        stored_roster_ev_path,
    )

    source_key = source.lower()
    if source_key not in {"civix", "legacy"}:
        typer.echo(f"Error: --source must be 'civix' or 'legacy', got {source!r}.", err=True)
        raise typer.Exit(code=1)

    roster_path = stored_roster_ev_path(data_dir, source_key, election_id)
    if not roster_path.exists():
        typer.echo(f"Error: roster file not found: {roster_path}", err=True)
        raise typer.Exit(code=1)

    parsed_date = (
        _parse_ev_date(ev_date) if ev_date is not None else report_date_from_roster_csv(roster_path)
    )

    records = read_roster_csv(roster_path)
    report_dates = {r.report_date for r in records}
    turnout = load_stored_turnout_for_audit(
        data_dir,
        source_key,
        election_id,
        report_dates=report_dates or None,
    )
    report = audit_records(
        records,
        turnout=turnout,
        election_id=election_id,
        report_date=parsed_date,
        source=source_key,
    )

    if output == "json":
        typer.echo(json.dumps(report.model_dump(mode="json"), indent=2))
    else:
        typer.echo(
            f"Audit Report — Election {report.election_id}  |  "
            f"{report.report_date}  |  {report.source}"
        )
        typer.echo(f"  Total records        : {report.total_records:,}")
        typer.echo(f"  Unique VUIDs         : {report.unique_vuids:,}")
        typer.echo(f"  Duplicate VUIDs      : {report.duplicate_vuid_count:,}")
        typer.echo(f"  Cross-method dups    : {report.cross_method_duplicate_count:,}")
        typer.echo(f"  Findings             : {len(report.findings)}")
        if report.findings:
            typer.echo("")
            for f in report.findings:
                county_str = f"  [{f.county}]" if f.county else ""
                typer.echo(f"  [{f.severity.upper()}] {f.finding_type}{county_str}: {f.detail}")

    # Write audit JSON to data directory
    audit_path = stored_audit_ev_path(data_dir, source_key, election_id)
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(json.dumps(report.model_dump(mode="json"), indent=2))
    typer.echo(f"\nAudit report written to {audit_path}")


@audit_app.command("run-inline")
def audit_run_inline(
    csv_path: Annotated[Path, typer.Argument(help="Path to roster CSV file")],
    election_id: Annotated[
        str | None, typer.Option("--election-id", help="Election ID override")
    ] = None,
    output: Annotated[
        str, typer.Option("--output", "-o", help="Output format: 'table' or 'json'")
    ] = "table",
) -> None:
    """Run data quality audit inline on a CSV file (no stored roster required)."""
    from ..audit import audit_from_csv

    if not csv_path.exists():
        typer.echo(f"Error: file not found: {csv_path}", err=True)
        raise typer.Exit(code=1)

    # Infer election_id and date from filename if not provided (e.g. roster_2024-10-21.csv)
    stem = csv_path.stem  # e.g. "roster_2024-10-21"
    inferred_date: dt.date | None = None
    inferred_id: str | None = election_id

    parts = stem.split("_")
    for part in parts:
        try:
            inferred_date = dt.date.fromisoformat(part)
        except ValueError:
            pass

    if inferred_date is None:
        inferred_date = dt.datetime.now(dt.timezone.utc).date()

    if inferred_id is None:
        inferred_id = "unknown"

    report = audit_from_csv(
        csv_path=csv_path,
        election_id=inferred_id,
        report_date=inferred_date,
        source="inline",
    )

    if output == "json":
        typer.echo(json.dumps(report.model_dump(mode="json"), indent=2))
    else:
        typer.echo(f"Audit Report (inline) — {csv_path.name}")
        typer.echo(f"  Total records        : {report.total_records:,}")
        typer.echo(f"  Unique VUIDs         : {report.unique_vuids:,}")
        typer.echo(f"  Duplicate VUIDs      : {report.duplicate_vuid_count:,}")
        typer.echo(f"  Cross-method dups    : {report.cross_method_duplicate_count:,}")
        typer.echo(f"  Findings             : {len(report.findings)}")
        if report.findings:
            typer.echo("")
            for f in report.findings:
                county_str = f"  [{f.county}]" if f.county else ""
                typer.echo(f"  [{f.severity.upper()}] {f.finding_type}{county_str}: {f.detail}")
