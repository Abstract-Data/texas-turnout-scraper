"""Voterfile matching CLI subcommands."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Annotated

import typer

from ._typer_apps import voterfile_app

_STANDARD_FIELDS = {
    "vuid": "Texas Voter Unique ID (join key — REQUIRED)",
    "cd": "Congressional District",
    "hd": "State House District",
    "sd": "State Senate District",
    "county": "County",
    "precinct": "Precinct",
    "last_name": "Last Name",
    "first_name": "First Name",
    "full_name": "Full Name (alternative to first/last)",
    "dob": "Date of Birth (for age bracket — YYYYMMDD or YYYY-MM-DD)",
    "sex": "Sex / Gender",
    "hispanic": "Hispanic flag",
    "status": "Voter registration status (V=active, S=suspense)",
}


@voterfile_app.command("match")
def voterfile_match(
    roster_csv: Annotated[
        Path, typer.Argument(help="EV roster CSV produced by 'fetch-all' or 'writer.py'")
    ],
    voterfile: Annotated[Path, typer.Argument(help="Statewide voterfile CSV")],
    output_dir: Annotated[
        Path | None, typer.Option("--output-dir", "-d", help="Directory for output files")
    ] = None,
    mapping_file: Annotated[
        Path | None,
        typer.Option("--mapping-file", "-m", help="Path to a saved column-mapping JSON"),
    ] = None,
    save_mapping: Annotated[
        bool,
        typer.Option(
            "--save-mapping/--no-save-mapping", help="Save detected mapping as a sidecar .json"
        ),
    ] = True,
    no_interactive: Annotated[
        bool,
        typer.Option("--no-interactive", help="Accept auto-detected mapping without prompting"),
    ] = False,
    redetect: Annotated[
        bool,
        typer.Option(
            "--redetect", help="Re-scan voterfile headers instead of loading a saved sidecar"
        ),
    ] = False,
    count_voterfile: Annotated[
        bool,
        typer.Option("--count-voterfile", help="Count all voterfile rows (slow on large files)"),
    ] = False,
    report_only: Annotated[
        bool,
        typer.Option("--report-only", help="Skip enriched CSV output; write match report only"),
    ] = False,
    gap_report: Annotated[
        bool,
        typer.Option(
            "--gap-report/--no-gap-report",
            help="Include turnout vs roster gap analysis in the match summary",
        ),
    ] = True,
    gap_turnout_source: Annotated[
        str,
        typer.Option(
            "--gap-turnout-source",
            help="Turnout source for gap analysis: live, stored, or auto",
        ),
    ] = "auto",
) -> None:
    """Match an EV roster against a statewide voterfile.

    Runs an interactive walkthrough to confirm column mappings, then uses
    DuckDB to join the voterfile (even multi-GB files) against the roster.

    Outputs:
      - {output_dir}/matched_{roster_stem}.csv   — EV roster enriched with CD, HD, SD,
                                                    age bracket, sex, hispanic flag
      - {output_dir}/match_report_{roster_stem}.json — audit summary

    \\b
    Example:
      tx-turnout voterfile match roster_ev_53813.csv texasmay2026.csv
    """
    from ..voterfile import (
        detect_columns,
        list_voterfile_columns,
        load_mapping,
        mapping_column_conflicts,
        match_voterfile_to_roster,
        sidecar_path_for,
        write_enriched_csv,
        write_match_report_json,
    )
    from ..voterfile import (
        save_mapping as _save_mapping,
    )
    from ..writer import read_roster_csv

    # Validate inputs
    if not roster_csv.exists():
        typer.echo(f"Error: roster file not found: {roster_csv}", err=True)
        raise typer.Exit(code=1)
    if not voterfile.exists():
        typer.echo(f"Error: voterfile not found: {voterfile}", err=True)
        raise typer.Exit(code=1)

    out_dir = output_dir or roster_csv.parent

    # ── Step 1: Load the EV roster ──────────────────────────────────────────
    typer.echo("")
    typer.echo("━" * 60)
    typer.echo("  Texas Turnout — Voterfile Match")
    typer.echo("━" * 60)
    typer.echo(f"  Roster:     {roster_csv.name}")
    typer.echo(f"  Voterfile:  {voterfile.name}")
    typer.echo("")

    typer.echo("Loading EV roster...", nl=False)
    roster_records = read_roster_csv(roster_csv)
    typer.echo(f"  {len(roster_records):,} records")

    # ── Step 2: Column mapping ───────────────────────────────────────────────
    sidecar = mapping_file or sidecar_path_for(voterfile)

    if sidecar.exists() and not redetect:
        mapping = load_mapping(sidecar)
        typer.echo(f"Loaded saved column mapping from {sidecar.name}")
        confidence = {
            f: ("✓ Saved" if getattr(mapping, f) else "✗ Not mapped") for f in _STANDARD_FIELDS
        }
    else:
        if redetect and sidecar.exists():
            typer.echo("Re-scanning voterfile columns (--redetect)...", nl=False)
        else:
            typer.echo("Scanning voterfile columns...", nl=False)
        mapping, confidence = detect_columns(voterfile)
        typer.echo("  done")

    # Display the mapping table
    typer.echo("")
    typer.echo("  Detected column mappings:")
    typer.echo(f"  {'Standard Field':<16}  {'Voterfile Column':<24}  Confidence")
    typer.echo(f"  {'─' * 16}  {'─' * 24}  {'─' * 14}")
    all_columns = list_voterfile_columns(voterfile)
    for field, _description in _STANDARD_FIELDS.items():
        col = getattr(mapping, field)
        conf = confidence.get(field, "")
        col_display = col or "(none)"
        typer.echo(f"  {field:<16}  {col_display:<24}  {conf}")
    typer.echo("")

    # ── Step 3: Interactive confirmation / override ──────────────────────────
    if not no_interactive:
        accept_all = typer.confirm("Accept these mappings?", default=True)
        if not accept_all:
            typer.echo("")
            typer.echo("Enter the voterfile column name for each field, or press Enter to skip.")
            typer.echo(f"Available columns: {', '.join(all_columns)}")
            typer.echo("")

            mapping_dict = mapping.model_dump()
            for field, description in _STANDARD_FIELDS.items():
                current = getattr(mapping, field) or ""
                prompt = f"  {field} ({description})"
                if current:
                    prompt += f" [current: {current}]"
                entered = typer.prompt(prompt, default=current or "", show_default=bool(current))
                entered = entered.strip()
                # Validate that the entered column exists
                if entered and entered not in all_columns:
                    typer.echo(
                        f"    Warning: '{entered}' not found in voterfile columns — skipping.",
                        err=True,
                    )
                    entered = ""
                mapping_dict[field] = entered or None

            from ..models import ColumnMapping as _CM2

            mapping = _CM2(**mapping_dict)

    # Check VUID is mapped
    if not mapping.vuid:
        typer.echo(
            "Error: VUID column is required for matching. Please map it to a voterfile column.",
            err=True,
        )
        raise typer.Exit(code=1)

    col_conflicts = mapping_column_conflicts(mapping)
    if col_conflicts:
        typer.echo("Error: column mapping conflict:", err=True)
        for msg in col_conflicts:
            typer.echo(f"  {msg}", err=True)
        raise typer.Exit(code=1)

    # ── Step 4: Run the match ────────────────────────────────────────────────
    typer.echo("")
    typer.echo(f"Running DuckDB match on {voterfile.name}...")
    typer.echo("  (scanning for matching VUIDs - this may take 30-60 seconds)")
    if count_voterfile:
        typer.echo("  (full voterfile row count enabled — may add extra time)")

    def _on_progress() -> None:
        typer.echo("  Scan complete.")

    try:
        enriched, report = match_voterfile_to_roster(
            roster_records=roster_records,
            voterfile_path=voterfile,
            mapping=mapping,
            progress_callback=_on_progress,
            count_voterfile=count_voterfile,
        )
    except (ImportError, ValueError, OSError) as exc:
        typer.echo(f"Error: match failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    report.roster_path = str(roster_csv)
    stem = roster_csv.stem

    gap_report_path: Path | None = None
    gap_counties_path: Path | None = None
    if gap_report:
        if gap_turnout_source not in {"live", "stored", "auto"}:
            typer.echo("Error: --gap-turnout-source must be live, stored, or auto.", err=True)
            raise typer.Exit(code=1)
        from ..gap_analysis import (
            try_build_civix_gap_report,
            write_gap_counties_csv,
            write_gap_report_json,
        )

        gap = try_build_civix_gap_report(
            roster_path=roster_csv,
            roster_records=roster_records,
            turnout_source=gap_turnout_source,
        )
        if gap is not None:
            report.turnout_roster_gap = gap
            gap_report_path = out_dir / f"gap_report_{stem}.json"
            gap_counties_path = out_dir / f"gap_counties_{stem}.csv"
            write_gap_report_json(gap, gap_report_path)
            write_gap_counties_csv(gap, gap_counties_path)

    # Save mapping sidecar after a successful match
    if save_mapping:
        mapping.voterfile_path = str(voterfile)
        if not mapping.created_at:
            mapping.created_at = dt.datetime.now(dt.timezone.utc).isoformat()
        _save_mapping(mapping, sidecar)
        typer.echo(f"  Column mapping saved to {sidecar.name}")

    # ── Step 5: Output ───────────────────────────────────────────────────────
    report_path = out_dir / f"match_report_{stem}.json"
    write_match_report_json(report, report_path)

    if not report_only:
        enriched_path = out_dir / f"matched_{stem}.csv"
        write_enriched_csv(enriched, enriched_path)

    # ── Step 6: Print summary ────────────────────────────────────────────────
    from ..terminal_report import print_voterfile_match_summary

    print_voterfile_match_summary(
        report,
        report_path=report_path,
        enriched_path=enriched_path if not report_only else None,
        gap_report_path=gap_report_path,
        gap_counties_path=gap_counties_path,
    )


@voterfile_app.command("detect-columns")
def voterfile_detect_columns(
    voterfile: Annotated[Path, typer.Argument(help="Statewide voterfile CSV to inspect")],
) -> None:
    """Show auto-detected column mappings for a voterfile without running a match."""
    from ..voterfile import detect_columns, list_voterfile_columns

    if not voterfile.exists():
        typer.echo(f"Error: voterfile not found: {voterfile}", err=True)
        raise typer.Exit(code=1)

    all_columns = list_voterfile_columns(voterfile)
    mapping, confidence = detect_columns(voterfile)

    typer.echo("")
    typer.echo(f"Voterfile: {voterfile.name}")
    typer.echo(f"Total columns: {len(all_columns)}")
    typer.echo("")
    typer.echo(f"  {'Field':<16}  {'Detected Column':<28}  Confidence")
    typer.echo(f"  {'─' * 16}  {'─' * 28}  {'─' * 14}")
    for field in _STANDARD_FIELDS:
        col = getattr(mapping, field) or "(not detected)"
        conf = confidence.get(field, "")
        typer.echo(f"  {field:<16}  {col:<28}  {conf}")

    typer.echo("")
    typer.echo("All voterfile columns:")
    for col in all_columns:
        typer.echo(f"  {col}")
