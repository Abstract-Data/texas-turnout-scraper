"""Typer-based CLI for the texas-turnout-scraper package.

Binary: tx-turnout
Subcommand groups:
  civix   — Civix EVR portal (2025+)
  legacy  — Legacy SOS HTML portal (pre-2025)
  audit   — Data quality audit commands (root-level)

All source modules are imported lazily inside command functions to keep
startup time fast.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Annotated

import typer

app = typer.Typer(name="tx-turnout", help="Texas SOS early-voting data tool.")
civix_app = typer.Typer(help="Commands for the Civix EVR portal (2025+)")
legacy_app = typer.Typer(help="Commands for the legacy SOS portal (pre-2025)")
audit_app = typer.Typer(help="Data quality audit commands")
voterfile_app = typer.Typer(help="Voterfile matching — join a statewide voterfile against an EV roster")

app.add_typer(civix_app, name="civix")
app.add_typer(legacy_app, name="legacy")
app.add_typer(audit_app, name="audit")
app.add_typer(voterfile_app, name="voterfile")


# ---------------------------------------------------------------------------
# Civix subcommands
# ---------------------------------------------------------------------------


@civix_app.command("elections")
def civix_elections_list(
    output: Annotated[str, typer.Option("--output", "-o", help="Output format: 'table' or 'json'")] = "table",
) -> None:
    """List all elections in the Civix EVR system."""
    from .civix import CivixClient

    with CivixClient() as client:
        elections = client.list_elections()

    if output == "json":
        rows = [
            {
                "id": e.id,
                "source_election_id": e.source_election_id,
                "name": e.election_name,
                "election_date": str(e.election_date),
                "election_type": e.election_type.value,
                "certified": e.certified,
                "ev_dates_count": len(e.early_voting_dates),
                "counties_count": len(e.counties),
            }
            for e in elections
        ]
        typer.echo(json.dumps(rows, indent=2))
    else:
        # Table output via typer.echo (rich is available via Typer)
        header = f"{'ID':>8}  {'DATE':<12}  {'TYPE':<22}  {'CERT':<5}  {'EV DATES':>8}  NAME"
        typer.echo(header)
        typer.echo("-" * len(header))
        for e in elections:
            cert = "yes" if e.certified else "no"
            typer.echo(
                f"{e.id:>8}  {e.election_date!s:<12}  {e.election_type.value:<22}  "
                f"{cert:<5}  {len(e.early_voting_dates):>8}  {e.election_name}"
            )


@civix_app.command("turnout")
def civix_turnout_fetch(
    election_id: Annotated[int, typer.Argument(help="Civix election ID (integer)")],
    ev_date: Annotated[date, typer.Argument(formats=["%Y-%m-%d"], help="EV date in YYYY-MM-DD format")],
    output: Annotated[str, typer.Option("--output", "-o", help="Output format: 'table' or 'json'")] = "table",
) -> None:
    """Fetch county EV turnout table for a Civix election + date."""
    from .civix import CivixClient

    with CivixClient() as client:
        rows = client.fetch_ev_turnout(election_id=election_id, ev_date=ev_date)

    if output == "json":
        data = [r.model_dump(mode="json") for r in rows]
        typer.echo(json.dumps(data, indent=2))
    else:
        header = (
            f"{'COUNTY':<20}  {'REG VOTERS':>12}  {'ON DATE':>9}  "
            f"{'TOTAL IP':>10}  {'TOTAL MAIL':>11}  {'ROSTER':>6}"
        )
        typer.echo(header)
        typer.echo("-" * len(header))
        for r in rows:
            roster = "yes" if r.roster_available else "no"
            typer.echo(
                f"{r.county:<20}  {r.registered_voters:>12,}  {r.in_person_votes_on_date:>9,}  "
                f"{r.total_in_person_votes:>10,}  {r.total_mail_votes:>11,}  {roster:>6}"
            )
        typer.echo(f"\n{len(rows)} counties")


@civix_app.command("roster")
def civix_roster_fetch(
    election_id: Annotated[int, typer.Argument(help="Civix election ID (integer)")],
    ev_date: Annotated[date, typer.Argument(formats=["%Y-%m-%d"], help="EV date in YYYY-MM-DD format")],
    county: Annotated[str | None, typer.Option("--county", help="County name (e.g. HARRIS). Omit for all counties.")] = None,
    out_dir: Annotated[Path | None, typer.Option("--out-dir", help="Directory to save roster CSVs.")] = None,
) -> None:
    """Fetch EV voter rosters for all counties (or one county) for a Civix election + date."""
    from .civix import CivixClient

    with CivixClient() as client:
        elections = client.list_elections()

    election = next((e for e in elections if e.id == election_id), None)
    if election is None:
        typer.echo(f"Error: election {election_id} not found in Civix.", err=True)
        raise typer.Exit(code=1)

    target_counties = election.counties
    if county:
        target_counties = [c for c in target_counties if c.name.upper() == county.upper()]
        if not target_counties:
            typer.echo(f"Error: county '{county}' not found for election {election_id}.", err=True)
            raise typer.Exit(code=1)

    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)

    total_saved = 0
    with CivixClient() as client:
        for county_ref in target_counties:
            roster = client.fetch_county_roster(
                election_id=election_id,
                ev_date=ev_date,
                county_id=county_ref.county_id,
                county_name=county_ref.name,
            )
            typer.echo(
                f"{county_ref.name:<20}  {roster.total_voters:>8,} voters  "
                f"(in-person: {sum(1 for r in roster.records if r.voting_method.value == 'IN-PERSON'):,}, "
                f"mail-in: {sum(1 for r in roster.records if r.voting_method.value == 'MAIL-IN'):,})"
            )
            if out_dir is not None:
                import csv

                out_path = out_dir / f"roster_{ev_date}_{county_ref.name}.csv"
                with out_path.open("w", newline="") as fh:
                    writer = csv.writer(fh)
                    writer.writerow(["id_voter", "voting_method", "precinct"])
                    for rec in roster.records:
                        writer.writerow([rec.id_voter, rec.voting_method.value, rec.precinct])
                total_saved += 1

    if out_dir is not None:
        typer.echo(f"\nSaved {total_saved} roster file(s) to {out_dir}")


# ---------------------------------------------------------------------------
# Legacy subcommands
# ---------------------------------------------------------------------------


def _exit_on_legacy_api_error(exc: ValueError | RuntimeError) -> None:
    """Map legacy_api failures to a stderr message and non-zero exit."""
    typer.echo(f"Error: {exc}", err=True)
    raise typer.Exit(code=1)


@legacy_app.command("elections")
def legacy_elections_list(
    output: Annotated[str, typer.Option("--output", "-o", help="Output format: 'table' or 'json'")] = "table",
) -> None:
    """List all elections from the legacy SOS HTML portal."""
    from . import legacy_api

    elections = legacy_api.list_elections()

    if output == "json":
        data = [e.model_dump(mode="json") for e in elections]
        typer.echo(json.dumps(data, indent=2))
    else:
        header = f"{'SOURCE ID':>10}  {'TYPE':<22}  {'YEAR':>5}  NAME"
        typer.echo(header)
        typer.echo("-" * len(header))
        for e in elections:
            year = str(e.election_year) if e.election_year else "—"
            typer.echo(
                f"{e.source_election_id:>10}  {e.election_type.value:<22}  "
                f"{year:>5}  {e.election_name}"
            )


@legacy_app.command("turnout")
def legacy_turnout_fetch(
    source_election_id: Annotated[str, typer.Argument(help="Legacy SOS election ID string (e.g. '49664')")],
    ev_date: Annotated[date, typer.Argument(formats=["%Y-%m-%d"], help="EV date in YYYY-MM-DD format")],
    output: Annotated[str, typer.Option("--output", "-o", help="Output format: 'table' or 'json'")] = "table",
) -> None:
    """Fetch county EV turnout from the legacy SOS portal."""
    from . import legacy_api

    try:
        rows = legacy_api.fetch_county_turnout(
            source_election_id=source_election_id,
            ev_date=ev_date,
            http_backend="cloudscraper",
        )
    except (ValueError, RuntimeError) as exc:
        _exit_on_legacy_api_error(exc)

    if output == "json":
        data = [r.model_dump(mode="json") for r in rows]
        typer.echo(json.dumps(data, indent=2))
    else:
        header = (
            f"{'COUNTY':<20}  {'REG VOTERS':>12}  {'ON DATE':>9}  "
            f"{'TOTAL IP':>10}  {'TOTAL MAIL':>11}"
        )
        typer.echo(header)
        typer.echo("-" * len(header))
        for r in rows:
            typer.echo(
                f"{r.county:<20}  {r.registered_voters:>12,}  {r.in_person_votes_on_date:>9,}  "
                f"{r.total_in_person_votes:>10,}  {r.total_mail_votes:>11,}"
            )
        typer.echo(f"\n{len(rows)} counties")


@legacy_app.command("roster")
def legacy_roster_fetch(
    source_election_id: Annotated[str, typer.Argument(help="Legacy SOS election ID string (e.g. '49664')")],
    ev_date: Annotated[date, typer.Argument(formats=["%Y-%m-%d"], help="EV date in YYYY-MM-DD format")],
    strategy: Annotated[str, typer.Option("--strategy", help="Fetch strategy: 'A' (per-county loop) or 'B' (bulk ZIP)")] = "A",
    out_dir: Annotated[Path | None, typer.Option("--out-dir", help="Directory to save roster output.")] = None,
) -> None:
    """Fetch EV voter rosters from the legacy SOS portal.

    Strategy A: per-county loop (~255 requests, ≥1.0 s pacing). Default.
    Strategy B: single bulk ZIP download (~35 MB, streamed to disk).
    """
    strategy_upper = strategy.upper()
    if strategy_upper not in ("A", "B"):
        typer.echo(f"Error: --strategy must be 'A' or 'B', got '{strategy}'.", err=True)
        raise typer.Exit(code=1)

    from . import legacy_api

    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)

    if strategy_upper == "B":
        if out_dir is None:
            typer.echo("Error: Strategy B requires --out-dir.", err=True)
            raise typer.Exit(code=1)
        try:
            legacy_api.fetch_roster(
                source_election_id=source_election_id,
                ev_date=ev_date,
                strategy="B",
                out_dir=out_dir,
            )
        except (ValueError, RuntimeError) as exc:
            _exit_on_legacy_api_error(exc)
        typer.echo(f"Bulk ZIP saved under {out_dir}")
        return

    try:
        rosters = legacy_api.fetch_roster(
            source_election_id=source_election_id,
            ev_date=ev_date,
            strategy="A",
            out_dir=out_dir,
        )
    except (ValueError, RuntimeError) as exc:
        _exit_on_legacy_api_error(exc)

    total_voters = sum(r.total_voters for r in rosters)
    for r in rosters:
        typer.echo(f"{r.county:<20}  {r.total_voters:>8,} voters")

    typer.echo(f"\n{len(rosters)} counties  |  {total_voters:,} total voters")
    if out_dir is not None and rosters:
        typer.echo(f"Saved {len(rosters)} roster file(s) to {out_dir}")


# ---------------------------------------------------------------------------
# Audit subcommands (root-level, not namespaced under civix/legacy)
# ---------------------------------------------------------------------------


@audit_app.command("run")
def audit_run(
    election_id: Annotated[str, typer.Argument(help="Election ID (source_election_id string)")],
    ev_date: Annotated[date, typer.Argument(formats=["%Y-%m-%d"], help="EV date in YYYY-MM-DD format")],
    source: Annotated[str, typer.Option("--source", help="Data source: 'civix' or 'legacy'")] = "civix",
    data_dir: Annotated[Path, typer.Option("--data-dir", help="Root data directory")] = Path("data"),
    output: Annotated[str, typer.Option("--output", "-o", help="Output format: 'table' or 'json'")] = "table",
) -> None:
    """Run data quality audit on a stored roster."""
    from .audit import run_audit

    roster_path = data_dir / "elections" / election_id / f"roster_{ev_date}.csv"
    if not roster_path.exists():
        typer.echo(f"Error: roster file not found: {roster_path}", err=True)
        raise typer.Exit(code=1)

    report = run_audit(
        csv_path=roster_path,
        election_id=election_id,
        report_date=ev_date,
        source=source,
    )

    if output == "json":
        typer.echo(json.dumps(report.model_dump(mode="json"), indent=2))
    else:
        typer.echo(f"Audit Report — Election {report.election_id}  |  {report.report_date}  |  {report.source}")
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
    audit_path = data_dir / "elections" / election_id / f"audit_{ev_date}.json"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(json.dumps(report.model_dump(mode="json"), indent=2))
    typer.echo(f"\nAudit report written to {audit_path}")


@audit_app.command("run-inline")
def audit_run_inline(
    csv_path: Annotated[Path, typer.Argument(help="Path to roster CSV file")],
    election_id: Annotated[str | None, typer.Option("--election-id", help="Election ID override")] = None,
    output: Annotated[str, typer.Option("--output", "-o", help="Output format: 'table' or 'json'")] = "table",
) -> None:
    """Run data quality audit inline on a CSV file (no stored roster required)."""
    from .audit import run_audit

    if not csv_path.exists():
        typer.echo(f"Error: file not found: {csv_path}", err=True)
        raise typer.Exit(code=1)

    # Infer election_id and date from filename if not provided (e.g. roster_2024-10-21.csv)
    stem = csv_path.stem  # e.g. "roster_2024-10-21"
    inferred_date: date | None = None
    inferred_id: str | None = election_id

    parts = stem.split("_")
    for part in parts:
        try:
            from datetime import datetime

            inferred_date = datetime.strptime(part, "%Y-%m-%d").date()
        except ValueError:
            pass

    if inferred_date is None:
        from datetime import datetime

        inferred_date = datetime.utcnow().date()

    if inferred_id is None:
        inferred_id = "unknown"

    report = run_audit(
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


# ---------------------------------------------------------------------------
# Voterfile subcommands
# ---------------------------------------------------------------------------

_STANDARD_FIELDS = {
    "vuid":       "Texas Voter Unique ID (join key — REQUIRED)",
    "cd":         "Congressional District",
    "hd":         "State House District",
    "sd":         "State Senate District",
    "county":     "County",
    "precinct":   "Precinct",
    "last_name":  "Last Name",
    "first_name": "First Name",
    "full_name":  "Full Name (alternative to first/last)",
    "dob":        "Date of Birth (for age bracket — YYYYMMDD or YYYY-MM-DD)",
    "sex":        "Sex / Gender",
    "hispanic":   "Hispanic flag",
    "status":     "Voter registration status (V=active, S=suspense)",
}


@voterfile_app.command("match")
def voterfile_match(
    roster_csv: Annotated[Path, typer.Argument(help="EV roster CSV produced by 'fetch-all' or 'writer.py'")],
    voterfile: Annotated[Path, typer.Argument(help="Statewide voterfile CSV")],
    output_dir: Annotated[Path | None, typer.Option("--output-dir", "-d", help="Directory for output files")] = None,
    mapping_file: Annotated[Path | None, typer.Option("--mapping-file", "-m", help="Path to a saved column-mapping JSON")] = None,
    save_mapping: Annotated[bool, typer.Option("--save-mapping/--no-save-mapping", help="Save detected mapping as a sidecar .json")] = True,
    no_interactive: Annotated[bool, typer.Option("--no-interactive", help="Accept auto-detected mapping without prompting")] = False,
    report_only: Annotated[bool, typer.Option("--report-only", help="Skip enriched CSV output; write match report only")] = False,
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
    from .voterfile import (
        detect_columns,
        list_voterfile_columns,
        load_mapping,
        match_voterfile_to_roster,
        sidecar_path_for,
        write_enriched_csv,
        write_match_report_json,
    )
    from .voterfile import (
        save_mapping as _save_mapping,
    )
    from .writer import read_roster_csv

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

    if sidecar.exists():
        mapping = load_mapping(sidecar)
        typer.echo(f"Loaded saved column mapping from {sidecar.name}")
        confidence: dict[str, str] = {
            f: ("✓ Saved" if getattr(mapping, f) else "✗ Not mapped")
            for f in _STANDARD_FIELDS
        }
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
    for field, description in _STANDARD_FIELDS.items():
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
                    typer.echo(f"    Warning: '{entered}' not found in voterfile columns — skipping.", err=True)
                    entered = ""
                mapping_dict[field] = entered or None

            from .models import ColumnMapping as _CM2
            mapping = _CM2(**mapping_dict)

    # Check VUID is mapped
    if not mapping.vuid:
        typer.echo("Error: VUID column is required for matching. Please map it to a voterfile column.", err=True)
        raise typer.Exit(code=1)

    # Save mapping sidecar
    if save_mapping and not sidecar.exists():
        import datetime as _dt
        mapping.voterfile_path = str(voterfile)
        mapping.created_at = _dt.datetime.utcnow().isoformat()
        _save_mapping(mapping, sidecar)
        typer.echo(f"  Column mapping saved to {sidecar.name}")

    # ── Step 4: Run the match ────────────────────────────────────────────────
    typer.echo("")
    typer.echo(f"Running DuckDB match on {voterfile.name}...")
    typer.echo("  (scanning for matching VUIDs — this may take 30–60 seconds)")

    def _on_progress() -> None:
        typer.echo("  Scan complete.")

    enriched, report = match_voterfile_to_roster(
        roster_records=roster_records,
        voterfile_path=voterfile,
        mapping=mapping,
        progress_callback=_on_progress,
    )
    report.roster_path = str(roster_csv)

    # ── Step 5: Output ───────────────────────────────────────────────────────
    stem = roster_csv.stem
    report_path = out_dir / f"match_report_{stem}.json"
    write_match_report_json(report, report_path)

    if not report_only:
        enriched_path = out_dir / f"matched_{stem}.csv"
        write_enriched_csv(enriched, enriched_path)

    # ── Step 6: Print summary ────────────────────────────────────────────────
    typer.echo("")
    typer.echo("━" * 60)
    typer.echo("  Match Summary")
    typer.echo("━" * 60)
    typer.echo(f"  Roster records    : {report.total_roster_records:>10,}")
    typer.echo(f"  Matched           : {report.matched_count:>10,}  ({report.match_rate:.1%})")
    typer.echo(f"  Unmatched         : {report.unmatched_count:>10,}")
    typer.echo("")

    if report.by_age_bracket:
        typer.echo("  Age brackets (matched voters):")
        for bracket, count in sorted(report.by_age_bracket.items()):
            bar = "█" * min(40, count // max(1, report.matched_count // 40))
            typer.echo(f"    {bracket:<8}  {count:>8,}  {bar}")
        typer.echo("")

    if report.by_voting_method:
        typer.echo("  Voting method (matched voters):")
        for method, count in sorted(report.by_voting_method.items()):
            typer.echo(f"    {method:<12}  {count:>8,}")
        typer.echo("")

    if report.by_cd:
        typer.echo(f"  Congressional districts: {len(report.by_cd)} found")
    if report.by_hd:
        typer.echo(f"  State House districts:   {len(report.by_hd)} found")
    if report.by_sd:
        typer.echo(f"  State Senate districts:  {len(report.by_sd)} found")

    if report.findings:
        typer.echo("")
        typer.echo("  Audit findings:")
        for finding in report.findings:
            typer.echo(f"    [{finding.severity.upper()}] {finding.finding_type}: {finding.detail}")

    typer.echo("")
    if not report_only:
        typer.echo(f"  Enriched CSV  → {enriched_path}")
    typer.echo(f"  Match report  → {report_path}")
    typer.echo("")


@voterfile_app.command("detect-columns")
def voterfile_detect_columns(
    voterfile: Annotated[Path, typer.Argument(help="Statewide voterfile CSV to inspect")],
) -> None:
    """Show auto-detected column mappings for a voterfile without running a match."""
    from .voterfile import detect_columns, list_voterfile_columns

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


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    app()


if __name__ == "__main__":
    main()
