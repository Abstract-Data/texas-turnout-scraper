"""Legacy SOS portal CLI subcommands."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from ._common import (
    _exit_on_partial_fetch_failures,
    _parse_ev_date,
    _roster_is_fresh,
    _run_typer_command,
    _update_election_index,
)
from ._typer_apps import (
    _DEFAULT_INDEX_PATH,
    _FRESHNESS_HOURS,
    EvDateStr,
    legacy_app,
)


def _exit_on_legacy_api_error(exc: ValueError | RuntimeError) -> None:
    """Map legacy_api failures to a stderr message and non-zero exit."""
    typer.echo(f"Error: {exc}", err=True)
    raise typer.Exit(code=1)


@legacy_app.command("elections")
def legacy_elections_list(
    output: Annotated[
        str, typer.Option("--output", "-o", help="Output format: 'table' or 'json'")
    ] = "table",
) -> None:
    """List all elections from the legacy SOS HTML portal."""
    from .. import legacy_api

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
    source_election_id: Annotated[
        str, typer.Argument(help="Legacy SOS election ID string (e.g. '49664')")
    ],
    ev_date: EvDateStr,
    output: Annotated[
        str, typer.Option("--output", "-o", help="Output format: 'table' or 'json'")
    ] = "table",
) -> None:
    """Fetch county EV turnout from the legacy SOS portal."""
    from .. import legacy_api

    parsed_date = _parse_ev_date(ev_date)
    try:
        rows = legacy_api.fetch_county_turnout(
            source_election_id=source_election_id,
            ev_date=parsed_date,
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
    source_election_id: Annotated[
        str, typer.Argument(help="Legacy SOS election ID string (e.g. '49664')")
    ],
    ev_date: EvDateStr,
    strategy: Annotated[
        str,
        typer.Option("--strategy", help="Fetch strategy: 'A' (per-county loop) or 'B' (bulk ZIP)"),
    ] = "A",
    out_dir: Annotated[
        Path | None, typer.Option("--out-dir", help="Directory to save roster output.")
    ] = None,
) -> None:
    """Fetch EV voter rosters from the legacy SOS portal.

    Strategy A: per-county loop (~255 requests, ≥1.0 s pacing). Default.
    Strategy B: single bulk ZIP download (~35 MB, streamed to disk).
    """
    strategy_upper = strategy.upper()
    if strategy_upper not in ("A", "B"):
        typer.echo(f"Error: --strategy must be 'A' or 'B', got '{strategy}'.", err=True)
        raise typer.Exit(code=1)

    from .. import legacy_api

    parsed_date = _parse_ev_date(ev_date)
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)

    if strategy_upper == "B":
        if out_dir is None:
            typer.echo("Error: Strategy B requires --out-dir.", err=True)
            raise typer.Exit(code=1)
        try:
            legacy_api.fetch_roster(
                source_election_id=source_election_id,
                ev_date=parsed_date,
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
            ev_date=parsed_date,
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


@legacy_app.command("fetch-all")
def legacy_fetch_all(
    source_election_id: Annotated[
        str, typer.Argument(help="Legacy SOS election ID string (e.g. '49664')")
    ],
    output_dir: Annotated[
        Path,
        typer.Option("--output-dir", help="Base output directory"),
    ] = Path("data/elections/legacy"),
    pace: Annotated[
        float,
        typer.Option("--pace", help="Seconds between requests (minimum 1.0)"),
    ] = 1.0,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Print planned fetches without downloading rosters"),
    ] = False,
    county_ids: Annotated[
        str | None,
        typer.Option("--county-ids", help="Comma-separated county IDs (default: all)"),
    ] = None,
    audit: Annotated[
        bool,
        typer.Option("--audit", help="Write audit_ev_{id}.json after accumulation"),
    ] = False,
    index_path: Annotated[
        Path,
        typer.Option("--index-path", help="Election index JSON path"),
    ] = _DEFAULT_INDEX_PATH,
) -> None:
    """Fetch all EV dates for a legacy election into one combined roster CSV."""
    import logging

    from .. import legacy_api
    from ..audit import audit_records
    from ..http_transport import HTTP_FETCH_EXCEPTIONS, format_fetch_error
    from ..models import CountyRoster
    from ..roster import fetch_roster_strategy_a
    from ..session import LegacySession
    from ..turnout import extract_county_ids, fetch_ev_details_html
    from ..writer import accumulate_roster, load_stored_turnout_for_audit, write_roster_csv

    logger = logging.getLogger(__name__)
    filter_ids: set[str] | None = None
    if county_ids:
        filter_ids = {part.strip() for part in county_ids.split(",") if part.strip()}

    elections = legacy_api.list_elections(pace=pace)
    election = next(
        (e for e in elections if e.source_election_id == source_election_id),
        None,
    )
    if election is None:
        typer.echo(f"Error: election {source_election_id} not found.", err=True)
        raise typer.Exit(code=1)

    election_dir = output_dir / source_election_id
    roster_path = election_dir / f"roster_ev_{source_election_id}.csv"
    audit_path = election_dir / f"audit_ev_{source_election_id}.json"

    all_rosters: list[CountyRoster] = []
    fetch_failures: list[str] = []
    with LegacySession(pace_seconds=pace) as session:
        ev_dates = session.prime_election(source_election_id)

        if not ev_dates:
            typer.echo(
                f"Error: no EV dates found for election {source_election_id}.",
                err=True,
            )
            raise typer.Exit(code=1)

        typer.echo(f"Election: {election.election_name} ({source_election_id})")
        typer.echo(f"EV dates: {len(ev_dates)}")

        if dry_run:
            for ev in ev_dates:
                typer.echo(f"  [{ev.date}] would fetch counties")
            typer.echo(f"Would write: {roster_path}")
            if audit:
                typer.echo(f"Would write: {audit_path}")
            return

        for ev in ev_dates:
            ev_date = ev.date
            date_label = ev_date.isoformat()
            typer.echo(f"[{date_label}] Fetching counties...", nl=False)

            try:
                html = fetch_ev_details_html(session, source_election_id, ev_date)
            except (*HTTP_FETCH_EXCEPTIONS, ValueError, RuntimeError) as exc:
                detail = format_fetch_error(exc)
                fetch_failures.append(f"{date_label}: turnout HTML failed ({detail})")
                logger.warning(
                    "Turnout HTML fetch failed for election %s on %s: %s",
                    source_election_id,
                    date_label,
                    detail,
                )
                continue

            id_by_name = extract_county_ids(html)
            if not id_by_name:
                typer.echo("  warning: no county IDs in turnout HTML", err=True)
                fetch_failures.append(f"{date_label}: no county IDs in turnout HTML")
                logger.warning(
                    "No county IDs in turnout HTML for election %s on %s.",
                    source_election_id,
                    date_label,
                )
                continue

            county_names = {county_id: name for name, county_id in id_by_name.items()}
            target_ids = list(id_by_name.values())
            if filter_ids is not None:
                target_ids = [cid for cid in target_ids if cid in filter_ids]
                if not target_ids:
                    typer.echo("  warning: no counties match --county-ids filter", err=True)
                    fetch_failures.append(f"{date_label}: no counties match --county-ids filter")
                    logger.warning(
                        "No counties match filter for election %s on %s.",
                        source_election_id,
                        date_label,
                    )
                    continue

            date_rosters: list[CountyRoster] = []
            for county_id in target_ids:
                county_label = county_names.get(county_id, county_id)
                try:
                    county_rosters = fetch_roster_strategy_a(
                        session,
                        source_election_id,
                        ev_date,
                        [county_id],
                        pace_seconds=pace,
                        county_names=county_names,
                        skip_prime=True,
                    )
                    date_rosters.extend(county_rosters)
                except (*HTTP_FETCH_EXCEPTIONS, ValueError, RuntimeError) as exc:
                    detail = format_fetch_error(exc)
                    fetch_failures.append(f"{county_label}/{date_label}: {detail}")
                    logger.warning(
                        "County fetch failed for county_id=%s on %s.",
                        county_id,
                        date_label,
                    )
                    typer.echo(
                        f"\n  Warning: county {county_label} failed on {date_label}, continuing.",
                        err=True,
                    )

            date_records = sum(r.total_voters for r in date_rosters)
            all_rosters.extend(date_rosters)
            typer.echo(f"  done ({date_records:,} records)")

            if not date_rosters:
                fetch_failures.append(f"{date_label}: zero county rosters")
                typer.echo(
                    f"  Warning: 0 county rosters for {date_label}.",
                    err=True,
                )
                logger.warning(
                    "Zero county rosters for election %s on %s.",
                    source_election_id,
                    date_label,
                )

    if not all_rosters:
        typer.echo(
            "Error: no rosters fetched across any EV date.",
            err=True,
        )
        raise typer.Exit(code=1)

    typer.echo(f"Accumulating {sum(r.total_voters for r in all_rosters):,} records...")
    records = accumulate_roster(all_rosters)
    unique_vuids = len({r.id_voter for r in records})
    duplicate_flags = sum(1 for r in records if r.duplicate_flag)

    election_dir.mkdir(parents=True, exist_ok=True)
    write_roster_csv(records, roster_path)

    typer.echo(f"  Unique VUIDs: {unique_vuids:,}")
    typer.echo(f"  Duplicate flags: {duplicate_flags:,}")
    typer.echo(f"Wrote: {roster_path}")

    if audit:
        audit_report_date = max(r.report_date for r in records)
        data_root = (
            output_dir.parent.parent if output_dir.name in {"civix", "legacy"} else output_dir
        )
        turnout = load_stored_turnout_for_audit(
            data_root,
            "legacy",
            source_election_id,
            report_dates={r.report_date for r in records},
        )
        report = audit_records(
            records,
            turnout=turnout,
            election_id=source_election_id,
            report_date=audit_report_date,
            source="legacy",
        )
        audit_path.write_text(json.dumps(report.model_dump(mode="json"), indent=2))
        typer.echo(f"Wrote: {audit_path}")

    _update_election_index(
        index_path,
        legacy_output_dir=output_dir,
        legacy_elections=elections,
        refreshed_legacy_ids={source_election_id},
    )

    _exit_on_partial_fetch_failures(fetch_failures)


@legacy_app.command("refresh-all")
def legacy_refresh_all(
    output_dir: Annotated[
        Path,
        typer.Option("--output-dir", help="Base output directory"),
    ] = Path("data/elections/legacy"),
    index_path: Annotated[
        Path,
        typer.Option("--index-path", help="Election index JSON path"),
    ] = _DEFAULT_INDEX_PATH,
    pace: Annotated[
        float,
        typer.Option("--pace", help="Seconds between requests (minimum 1.0)"),
    ] = 1.0,
    audit: Annotated[
        bool,
        typer.Option("--audit", help="Write audit_ev JSON for refreshed elections"),
    ] = True,
    max_age_hours: Annotated[
        int,
        typer.Option("--max-age-hours", help="Skip elections refreshed within this many hours"),
    ] = _FRESHNESS_HOURS,
) -> None:
    """Refresh legacy elections that already have on-disk roster files."""
    from .. import legacy_api

    elections = legacy_api.list_elections(pace=pace)
    typer.echo(f"Legacy elections on portal: {len(elections)}")

    attempted = 0
    updated = 0
    skipped = 0
    failed = 0

    for election in elections:
        source_election_id = election.source_election_id
        roster_path = output_dir / source_election_id / f"roster_ev_{source_election_id}.csv"
        if not roster_path.exists():
            typer.echo(
                f"Skipping {source_election_id} ({election.election_name}) — no roster on disk",
            )
            skipped += 1
            continue
        if _roster_is_fresh(
            roster_path,
            index_path=index_path,
            source_prefix="legacy",
            election_id=source_election_id,
            max_age_hours=max_age_hours,
        ):
            typer.echo(f"Skipping {source_election_id} ({election.election_name}) — fresh")
            skipped += 1
            continue

        attempted += 1
        typer.echo(f"Refreshing {source_election_id} ({election.election_name})...")
        ok = _run_typer_command(
            legacy_fetch_all,
            source_election_id=source_election_id,
            output_dir=output_dir,
            pace=pace,
            dry_run=False,
            county_ids=None,
            audit=audit,
            index_path=index_path,
        )
        if ok:
            updated += 1
        else:
            failed += 1
            typer.echo(f"Failed to refresh {source_election_id}", err=True)

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
