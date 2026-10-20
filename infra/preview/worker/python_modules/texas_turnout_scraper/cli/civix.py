"""Civix EVR portal CLI subcommands."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from ._common import (
    _echo_civix_elections_table,
    _exit_on_partial_fetch_failures,
    _parse_ev_date,
    _prompt_civix_election_id,
    _prompt_civix_ev_date,
    _prompt_civix_scrape_action,
    _resolve_civix_election,
    _roster_is_fresh,
    _run_typer_command,
    _sort_civix_elections,
    _stdin_is_tty,
    _update_election_index,
)
from ._typer_apps import (
    _DEFAULT_INDEX_PATH,
    _FRESHNESS_HOURS,
    EvDateStr,
    civix_app,
)


def _run_civix_elections_interactive(elections: list[object]) -> None:
    """Prompt for election + scrape action, then dispatch to civix subcommands."""
    if not elections:
        typer.echo("No elections returned from Civix.", err=True)
        raise typer.Exit(code=1)

    election_id = _prompt_civix_election_id(elections)
    from ..models import CivixElection

    election = _resolve_civix_election(elections, election_id)
    if election is None or not isinstance(election, CivixElection):
        typer.echo(f"Error: election {election_id} not found.", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"\nSelected: {election.election_name} ({election_id})")

    action = _prompt_civix_scrape_action()
    if action == "done":
        return

    if action == "fetch_all":
        _run_typer_command(
            civix_fetch_all,
            election_id=election_id,
            output_dir=Path("data/elections/civix"),
            dry_run=False,
            write_audit=False,
        )
        return

    ev_date = _prompt_civix_ev_date(election)
    ev_date_str = ev_date.isoformat()

    if action == "turnout":
        _run_typer_command(
            civix_turnout_fetch,
            election_id=election_id,
            ev_date=ev_date_str,
        )
        return

    out_dir_raw = typer.prompt(
        "Output directory (blank = print only, no files)",
        default="",
        show_default=False,
    )
    out_dir = Path(out_dir_raw) if out_dir_raw.strip() else None
    strategy = "statewide" if action == "roster_statewide" else "per-county"
    _run_typer_command(
        civix_roster_fetch,
        election_id=election_id,
        ev_date=ev_date_str,
        county=None,
        out_dir=out_dir,
        strategy=strategy,
    )


# ---------------------------------------------------------------------------
# Civix subcommands
# ---------------------------------------------------------------------------


@civix_app.command("elections")
def civix_elections_list(
    output: Annotated[
        str, typer.Option("--output", "-o", help="Output format: 'table' or 'json'")
    ] = "table",
    interactive: Annotated[
        bool | None,
        typer.Option(
            "--interactive/--no-interactive",
            help=(
                "Prompt to select an election and scrape action (default: on when stdout is a TTY)"
            ),
        ),
    ] = None,
) -> None:
    """List Civix elections (newest first) and optionally run an interactive scrape wizard."""
    from ..civix import CivixClient
    from ..models import CivixElection

    with CivixClient() as client:
        elections = _sort_civix_elections(client.list_elections())

    use_interactive = interactive if interactive is not None else _stdin_is_tty()
    if use_interactive and output != "json":
        _run_civix_elections_interactive(elections)
        return

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
            if isinstance(e, CivixElection)
        ]
        typer.echo(json.dumps(rows, indent=2))
    else:
        _echo_civix_elections_table(elections, numbered=False)


@civix_app.command("turnout")
def civix_turnout_fetch(
    election_id: Annotated[
        str, typer.Argument(help="Civix election ID (numeric string, e.g. '53813')")
    ],
    ev_date: EvDateStr,
    output: Annotated[
        str, typer.Option("--output", "-o", help="Output format: 'table' or 'json'")
    ] = "table",
) -> None:
    """Fetch county EV turnout table for a Civix election + date."""
    from ..civix import CivixClient

    parsed_date = _parse_ev_date(ev_date)
    with CivixClient() as client:
        elections = client.list_elections()
        election = _resolve_civix_election(elections, election_id)
        if election is None:
            typer.echo(f"Error: election {election_id} not found in Civix.", err=True)
            raise typer.Exit(code=1)
        rows = client.fetch_ev_turnout(
            election_id=election.id,
            election_date=parsed_date,
        )

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
    election_id: Annotated[
        str, typer.Argument(help="Civix election ID (numeric string, e.g. '53813')")
    ],
    ev_date: EvDateStr,
    county: Annotated[
        str | None,
        typer.Option("--county", help="County name (e.g. HARRIS). Omit for all counties."),
    ] = None,
    out_dir: Annotated[
        Path | None, typer.Option("--out-dir", help="Directory to save roster CSVs.")
    ] = None,
    strategy: Annotated[
        str,
        typer.Option(
            "--strategy",
            help="Roster fetch: 'per-county' (CSV per county) or 'statewide' (single bulk file)",
        ),
    ] = "per-county",
) -> None:
    """Fetch EV voter rosters for all counties (or one county) for a Civix election + date."""
    from ..civix import CivixClient, fetch_county_roster

    strategy_normalized = strategy.strip().lower().replace("_", "-")
    if strategy_normalized not in ("per-county", "statewide"):
        typer.echo(
            f"Error: --strategy must be 'per-county' or 'statewide', got {strategy!r}.",
            err=True,
        )
        raise typer.Exit(code=1)

    parsed_date = _parse_ev_date(ev_date)
    with CivixClient() as client:
        elections = client.list_elections()

    election = _resolve_civix_election(elections, election_id)
    if election is None:
        typer.echo(f"Error: election {election_id} not found in Civix.", err=True)
        raise typer.Exit(code=1)

    if strategy_normalized == "statewide":
        if county is not None:
            typer.echo("Error: --county is not supported with --strategy statewide.", err=True)
            raise typer.Exit(code=1)
        save_dir = out_dir if out_dir is not None else Path.cwd()
        save_dir.mkdir(parents=True, exist_ok=True)
        with CivixClient() as client:
            payload = client.fetch_statewide(
                election_id=election.id,
                election_date=parsed_date,
            )
        suffix = ".zip" if payload[:2] == b"PK" else ".csv"
        output_path = save_dir / f"statewide_{election_id}_{parsed_date.isoformat()}{suffix}"
        output_path.write_bytes(payload)
        typer.echo(f"Saved {len(payload):,} bytes to {output_path}")
        return

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
            roster = fetch_county_roster(
                client,
                election_id=election.id,
                election_date=parsed_date,
                county_name=county_ref.name,
                county_id=county_ref.county_id,
            )
            typer.echo(
                f"{county_ref.name:<20}  {roster.total_voters:>8,} voters  "
                f"(in-person: {roster.in_person_count:,}, "
                f"mail-in: {roster.mail_in_count:,})"
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


@civix_app.command("fetch-all")
def civix_fetch_all(
    election_id: Annotated[
        str, typer.Argument(help="Civix election ID (numeric string, e.g. '53813')")
    ],
    output_dir: Annotated[
        Path,
        typer.Option("--output-dir", help="Base output directory"),
    ] = Path("data/elections/civix"),
    pace: Annotated[
        float, typer.Option("--pace", help="Seconds between requests (minimum 1.0)")
    ] = 1.0,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Print fetch plan without roster CSV requests")
    ] = False,
    write_audit: Annotated[
        bool,
        typer.Option("--audit", help="Run audit_records and write audit_ev JSON"),
    ] = False,
    index_path: Annotated[
        Path,
        typer.Option("--index-path", help="Election index JSON path"),
    ] = _DEFAULT_INDEX_PATH,
) -> None:
    """Fetch all EV dates for a Civix election into one per-election roster CSV."""
    import logging

    from ..audit import audit_records
    from ..civix import CivixClient, fetch_county_roster
    from ..http_transport import HTTP_FETCH_EXCEPTIONS, format_fetch_error
    from ..models import CountyRoster, CountyTurnout, VoterRecord
    from ..writer import (
        accumulate_roster,
        load_stored_turnout_for_audit,
        stored_ed_turnout_path,
        stored_roster_ed_path,
        stored_statewide_ed_zip_path,
        write_roster_csv,
        write_turnout_csv,
    )

    logger = logging.getLogger(__name__)
    with CivixClient(pace_seconds=pace) as client:
        elections = client.list_elections()

    election = _resolve_civix_election(elections, election_id)
    if election is None:
        typer.echo(f"Election {election_id} not found", err=True)
        raise typer.Exit(code=1)

    civix_id = election.id
    ev_dates = election.early_voting_dates
    output_path = output_dir / election_id / f"roster_ev_{election_id}.csv"
    ed_turnout_path = stored_ed_turnout_path(output_dir, election_id, election.election_date)
    ed_roster_path = stored_roster_ed_path(output_dir, election_id, election.election_date)
    ed_statewide_zip_path = stored_statewide_ed_zip_path(
        output_dir, election_id, election.election_date
    )

    typer.echo(f"Election: {election.election_name} ({election_id})")
    typer.echo(f"EV dates: {len(ev_dates)}")

    if dry_run:
        county_count = len(election.counties)
        for ev in ev_dates:
            typer.echo(
                f"[{ev.date}] Would fetch up to {county_count} counties "
                "(roster availability not checked in dry-run)...",
            )
        typer.echo(f"Would write: {output_path}")
        typer.echo(
            f"[{election.election_date}] Would fetch Election Day turnout → {ed_turnout_path}"
        )
        typer.echo(
            f"[{election.election_date}] Would fetch Election Day statewide ZIP "
            f"→ {ed_statewide_zip_path}"
        )
        typer.echo(f"[{election.election_date}] Would write Election Day roster → {ed_roster_path}")
        return

    all_rosters = []
    fetch_failures: list[str] = []
    with CivixClient(pace_seconds=pace) as client:
        for ev in ev_dates:
            ev_date = ev.date
            turnout_rows = client.fetch_ev_turnout(
                election_id=civix_id,
                election_date=ev_date,
            )
            turnout_path = output_dir / election_id / f"turnout_ev_{ev_date.isoformat()}.csv"
            write_turnout_csv(
                [CountyTurnout(**row.model_dump()) for row in turnout_rows],
                turnout_path,
            )
            roster_counties = [r for r in turnout_rows if r.roster_available]
            typer.echo(
                f"[{ev_date}] Fetching {len(roster_counties)} counties...",
                nl=False,
            )

            date_rosters = []
            for county_row in roster_counties:
                try:
                    roster = fetch_county_roster(
                        client,
                        election_id=civix_id,
                        election_date=ev_date,
                        county_name=county_row.county,
                        county_id=county_row.county_id,
                    )
                    date_rosters.append(roster)
                except (*HTTP_FETCH_EXCEPTIONS, ValueError, RuntimeError) as exc:
                    detail = format_fetch_error(exc)
                    fetch_failures.append(f"{county_row.county}/{ev_date}: {detail}")
                    logger.warning(
                        "County roster fetch failed for %s on %s: %s",
                        county_row.county,
                        ev_date,
                        detail,
                    )

            date_records = sum(len(r.records) for r in date_rosters)
            if not date_rosters:
                fetch_failures.append(f"{ev_date}: no county rosters fetched")
                logger.warning("No rosters fetched for EV date %s", ev_date)
            typer.echo(f"  done ({date_records:,} records)")
            all_rosters.extend(date_rosters)

        typer.echo(
            f"[{election.election_date}] Fetching Election Day turnout...",
            nl=False,
        )
        try:
            ed_turnout_rows = client.fetch_ed_turnout(
                election_id=civix_id,
                election_date=election.election_date,
            )
        except (*HTTP_FETCH_EXCEPTIONS, ValueError, RuntimeError) as exc:
            detail = format_fetch_error(exc)
            typer.echo(f"  unavailable ({detail})")
            logger.info(
                "Election Day turnout unavailable for election %s on %s: %s",
                election_id,
                election.election_date,
                detail,
            )
        else:
            if ed_turnout_rows:
                write_turnout_csv(
                    [CountyTurnout(**row.model_dump()) for row in ed_turnout_rows],
                    ed_turnout_path,
                )
                typer.echo(f"  done ({len(ed_turnout_rows):,} counties → {ed_turnout_path.name})")
            else:
                typer.echo("  no data yet")

            if ed_turnout_rows:
                typer.echo(
                    f"[{election.election_date}] Fetching Election Day statewide roster...",
                    nl=False,
                )
                try:
                    ed_zip_bytes = client.fetch_ed_statewide_zip(
                        election_id=civix_id,
                        election_date=election.election_date,
                    )
                except (*HTTP_FETCH_EXCEPTIONS, ValueError, RuntimeError) as exc:
                    detail = format_fetch_error(exc)
                    typer.echo(f"  unavailable ({detail})")
                    fetch_failures.append(
                        f"election-day-statewide/{election.election_date}: {detail}"
                    )
                    logger.warning(
                        "Election Day statewide roster unavailable for election %s on %s: %s",
                        election_id,
                        election.election_date,
                        detail,
                    )
                else:
                    ed_statewide_zip_path.parent.mkdir(parents=True, exist_ok=True)
                    ed_statewide_zip_path.write_bytes(ed_zip_bytes)
                    from ..civix import parse_ed_statewide_voter_records

                    ed_records = parse_ed_statewide_voter_records(
                        ed_zip_bytes,
                        election_id=election_id,
                        report_date=election.election_date,
                    )
                    ed_by_county: dict[str, list[VoterRecord]] = {}
                    for rec in ed_records:
                        ed_by_county.setdefault(rec.county, []).append(rec)
                    ed_county_rosters = [
                        CountyRoster(
                            county=county,
                            county_id=next(
                                (row.county_id for row in ed_turnout_rows if row.county == county),
                                None,
                            ),
                            election_id=election_id,
                            report_date=election.election_date,
                            source="civix",
                            records=county_records,
                        )
                        for county, county_records in ed_by_county.items()
                    ]
                    flagged_ed = accumulate_roster(ed_county_rosters)
                    write_roster_csv(flagged_ed, ed_roster_path)
                    typer.echo(
                        f"  done ({len(flagged_ed):,} records → "
                        f"{ed_statewide_zip_path.name}, {ed_roster_path.name})"
                    )

    if not all_rosters:
        typer.echo("Error: no rosters fetched across any EV date", err=True)
        raise typer.Exit(code=1)

    typer.echo(f"Accumulating {sum(len(r.records) for r in all_rosters):,} records...")
    records = accumulate_roster(all_rosters)
    unique_vuids = len({r.id_voter for r in records})
    duplicate_flags = sum(1 for r in records if r.duplicate_flag)
    typer.echo(f"  Unique VUIDs: {unique_vuids:,}")
    typer.echo(f"  Duplicate flags: {duplicate_flags:,}")

    write_roster_csv(records, output_path)
    typer.echo(f"Wrote: {output_path}")

    if write_audit:
        audit_report_date = max(r.report_date for r in records)
        data_root = (
            output_dir.parent.parent if output_dir.name in {"civix", "legacy"} else output_dir
        )
        turnout = load_stored_turnout_for_audit(
            data_root,
            "civix",
            election_id,
            report_dates={r.report_date for r in records},
        )
        report = audit_records(
            records,
            turnout=turnout,
            election_id=election_id,
            report_date=audit_report_date,
            source="civix",
        )
        audit_path = output_dir / election_id / f"audit_ev_{election_id}.json"
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        audit_path.write_text(json.dumps(report.model_dump(mode="json"), indent=2))
        typer.echo(f"Audit report: {audit_path}")

    _update_election_index(
        index_path,
        civix_output_dir=output_dir,
        civix_elections=elections,
        refreshed_civix_ids={election_id},
    )

    _exit_on_partial_fetch_failures(fetch_failures)


def _echo_gap_report_summary(report: object) -> None:
    from ..models import TurnoutRosterGapReport
    from ..terminal_report import print_gap_report_summary

    if isinstance(report, TurnoutRosterGapReport):
        print_gap_report_summary(report)


@civix_app.command("gap-report")
def civix_gap_report(
    election_id: Annotated[
        str, typer.Argument(help="Civix election ID (numeric string, e.g. '58315')")
    ],
    roster: Annotated[
        Path | None,
        typer.Option("--roster", help="Combined roster CSV (default: stored roster_ev file)"),
    ] = None,
    ev_date: EvDateStr | None = None,
    output_dir: Annotated[
        Path,
        typer.Option("--output-dir", help="Election data directory"),
    ] = Path("data/elections/civix"),
    turnout_source: Annotated[
        str,
        typer.Option(
            "--turnout-source",
            help="Turnout data source: live (API), stored (saved CSV), or auto",
        ),
    ] = "auto",
    output: Annotated[
        str, typer.Option("--output", "-o", help="Output format: 'table' or 'json'")
    ] = "table",
    write_files: Annotated[
        bool,
        typer.Option("--write-files/--no-write-files", help="Write JSON + county CSV reports"),
    ] = True,
) -> None:
    """Compare published Civix turnout vs scraped roster voters, county by county."""
    from ..gap_analysis import (
        stored_gap_counties_csv_path,
        stored_gap_report_path,
        try_build_civix_gap_report,
        write_gap_counties_csv,
        write_gap_report_json,
    )
    from ..writer import read_roster_csv

    if turnout_source not in {"live", "stored", "auto"}:
        typer.echo("Error: --turnout-source must be live, stored, or auto.", err=True)
        raise typer.Exit(code=1)

    roster_path = roster or (output_dir / election_id / f"roster_ev_{election_id}.csv")
    if not roster_path.exists():
        typer.echo(f"Error: roster file not found: {roster_path}", err=True)
        raise typer.Exit(code=1)

    roster_records = read_roster_csv(roster_path)
    if not roster_records:
        typer.echo(f"Error: roster file is empty: {roster_path}", err=True)
        raise typer.Exit(code=1)

    data_root = (
        output_dir.parent.parent if output_dir.name in {"civix", "legacy"} else output_dir.parent
    )

    parsed_ev_date = (
        _parse_ev_date(ev_date)
        if ev_date is not None
        else max(rec.report_date for rec in roster_records)
    )
    report = try_build_civix_gap_report(
        roster_path=roster_path,
        roster_records=roster_records,
        ev_date=parsed_ev_date,
        turnout_source=turnout_source,
    )
    if report is None:
        typer.echo("Error: could not build gap report for this roster/election.", err=True)
        raise typer.Exit(code=1)

    if write_files:
        json_path = stored_gap_report_path(data_root, "civix", election_id)
        csv_path = stored_gap_counties_csv_path(data_root, "civix", election_id)
        write_gap_report_json(report, json_path)
        write_gap_counties_csv(report, csv_path)
        typer.echo(f"Wrote: {json_path}")
        typer.echo(f"Wrote: {csv_path}")

    if output == "json":
        typer.echo(json.dumps(report.model_dump(mode="json"), indent=2))
    else:
        _echo_gap_report_summary(report)


@civix_app.command("refresh-all")
def civix_refresh_all(
    output_dir: Annotated[
        Path,
        typer.Option("--output-dir", help="Base output directory"),
    ] = Path("data/elections/civix"),
    index_path: Annotated[
        Path,
        typer.Option("--index-path", help="Election index JSON path"),
    ] = _DEFAULT_INDEX_PATH,
    pace: Annotated[
        float, typer.Option("--pace", help="Seconds between requests (minimum 1.0)")
    ] = 1.0,
    write_audit: Annotated[
        bool,
        typer.Option("--audit", help="Write audit_ev JSON for refreshed elections"),
    ] = True,
    max_age_hours: Annotated[
        int,
        typer.Option("--max-age-hours", help="Skip elections refreshed within this many hours"),
    ] = _FRESHNESS_HOURS,
) -> None:
    """Discover certified Civix elections and refresh stale roster files."""
    from ..civix import CivixClient

    with CivixClient(pace_seconds=pace) as client:
        elections = client.list_elections()

    certified = [e for e in elections if e.certified]
    typer.echo(f"Certified elections: {len(certified)}")

    attempted = 0
    updated = 0
    skipped = 0
    failed = 0

    for election in certified:
        election_id = election.source_election_id
        roster_path = output_dir / election_id / f"roster_ev_{election_id}.csv"
        if _roster_is_fresh(
            roster_path,
            index_path=index_path,
            source_prefix="civix",
            election_id=election_id,
            max_age_hours=max_age_hours,
        ):
            typer.echo(f"Skipping {election_id} ({election.election_name}) — fresh")
            skipped += 1
            continue

        attempted += 1
        typer.echo(f"Refreshing {election_id} ({election.election_name})...")
        ok = _run_typer_command(
            civix_fetch_all,
            election_id=election_id,
            output_dir=output_dir,
            pace=pace,
            dry_run=False,
            write_audit=write_audit,
            index_path=index_path,
        )
        if ok:
            updated += 1
        else:
            failed += 1
            typer.echo(f"Failed to refresh {election_id}", err=True)

    if updated == 0:
        typer.echo("Index unchanged (no elections refreshed)")

    typer.echo("")
    typer.echo("Refresh summary")
    typer.echo(f"  Updated : {updated}")
    typer.echo(f"  Skipped : {skipped}")
    typer.echo(f"  Failed  : {failed}")

    if failed > 0:
        typer.echo(f"Error: {failed} election refresh(es) failed", err=True)
        raise typer.Exit(code=1)
    if attempted > 0 and updated == 0:
        typer.echo("Error: all refresh attempts failed", err=True)
        raise typer.Exit(code=1)
