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

import datetime as dt
import json
from collections.abc import Callable
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


def _parse_ev_date(value: str | dt.date) -> dt.date:
    if isinstance(value, dt.date):
        return value
    try:
        return dt.datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise typer.BadParameter(
            f"Invalid EV date {value!r}; expected YYYY-MM-DD.",
        ) from exc


def _resolve_civix_election(elections: list[object], election_id: str) -> object | None:
    from .models import CivixElection

    return next(
        (
            e
            for e in elections
            if isinstance(e, CivixElection)
            and (str(e.id) == election_id or e.source_election_id == election_id)
        ),
        None,
    )


def _exit_on_partial_fetch_failures(failures: list[str]) -> None:
    if not failures:
        return
    typer.echo(
        f"Error: {len(failures)} partial fetch failure(s); roster may be incomplete.",
        err=True,
    )
    for item in failures[:20]:
        typer.echo(f"  - {item}", err=True)
    if len(failures) > 20:
        typer.echo(f"  ... and {len(failures) - 20} more", err=True)
    raise typer.Exit(code=1)


def _iso_now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_index_timestamp(value: object) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        if value.endswith("Z"):
            return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        parsed = dt.datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=dt.timezone.utc)
        return parsed
    except ValueError:
        return None


def _find_index_entry(
    index_path: Path,
    source_prefix: str,
    election_id: str,
) -> dict[str, object] | None:
    civix_entries, legacy_entries = _load_index_sections(index_path)
    entries = civix_entries if source_prefix == "civix" else legacy_entries
    for entry in entries:
        if str(entry.get("source_election_id")) == election_id:
            return entry
    return None


def _roster_is_fresh(
    roster_path: Path,
    *,
    index_path: Path | None = None,
    source_prefix: str = "civix",
    election_id: str = "",
    max_age_hours: int = _FRESHNESS_HOURS,
) -> bool:
    """Return True when a roster was refreshed within max_age_hours.

    Prefers ``last_refreshed`` from index.json (stable across CI checkouts) and
    falls back to the roster file mtime for local runs without index metadata.
    """
    if not roster_path.exists():
        return False
    if max_age_hours <= 0:
        return False

    import time

    now = dt.datetime.now(dt.timezone.utc)
    max_age = dt.timedelta(hours=max_age_hours)

    if index_path is not None and index_path.exists() and election_id:
        entry = _find_index_entry(index_path, source_prefix, election_id)
        if entry is not None:
            refreshed = _parse_index_timestamp(entry.get("last_refreshed"))
            if refreshed is not None:
                return now - refreshed < max_age

    age_seconds = time.time() - roster_path.stat().st_mtime
    return age_seconds < max_age_hours * 3600


def _roster_mtime_iso(path: Path) -> str:
    mtime = dt.datetime.fromtimestamp(
        path.stat().st_mtime,
        tz=dt.timezone.utc,
    )
    return mtime.strftime("%Y-%m-%dT%H:%M:%SZ")


def _election_index_entry_from_roster(
    *,
    source_election_id: str,
    election_name: str,
    election_date: dt.date,
    election_type: str,
    certified: bool | None,
    roster_path: Path,
    source_prefix: str,
    last_refreshed: str | None = None,
) -> dict[str, object]:
    from .audit import audit_records
    from .writer import read_roster_csv

    records = read_roster_csv(roster_path)
    report = audit_records(
        records,
        election_id=source_election_id,
        source=source_prefix,
    )
    entry: dict[str, object] = {
        "source_election_id": source_election_id,
        "election_name": election_name,
        "election_date": election_date.isoformat(),
        "election_type": election_type,
        "roster_path": f"{source_prefix}/{source_election_id}/roster_ev_{source_election_id}.csv",
        "last_refreshed": last_refreshed or _roster_mtime_iso(roster_path),
        "total_records": report.total_records,
        "unique_vuids": report.unique_vuids,
        "duplicate_vuid_count": report.duplicate_vuid_count,
    }
    if certified is not None:
        entry["certified"] = certified
    return entry


def _legacy_election_date(meta: object | None) -> dt.date:
    from .models import LegacyElection

    if isinstance(meta, LegacyElection):
        if meta.ev_dates:
            return meta.ev_dates[-1].date
        if meta.election_year is not None:
            return dt.date(meta.election_year, 11, 1)
    return dt.date.today()


def _existing_index_entries(index_path: Path | None) -> dict[str, dict[str, object]]:
    if index_path is None or not index_path.exists():
        return {}
    civix_entries, legacy_entries = _load_index_sections(index_path)
    combined = [*civix_entries, *legacy_entries]
    return {str(entry.get("source_election_id")): entry for entry in combined}


def _resolve_last_refreshed(
    election_id: str,
    roster_path: Path,
    *,
    existing_entries: dict[str, dict[str, object]],
    refreshed_ids: set[str],
) -> str:
    if election_id in refreshed_ids:
        return _iso_now()
    existing = existing_entries.get(election_id, {})
    prior = existing.get("last_refreshed")
    if isinstance(prior, str) and prior:
        return prior
    return _roster_mtime_iso(roster_path)


def _build_civix_index_entries(
    output_dir: Path,
    elections: list[object],
    *,
    index_path: Path | None = None,
    refreshed_ids: set[str] | None = None,
) -> list[dict[str, object]]:
    from .models import CivixElection

    by_id = {e.source_election_id: e for e in elections if isinstance(e, CivixElection)}
    existing_entries = _existing_index_entries(index_path)
    refreshed = refreshed_ids or set()
    entries: list[dict[str, object]] = []
    if not output_dir.exists():
        return entries

    for election_dir in sorted(output_dir.iterdir()):
        if not election_dir.is_dir():
            continue
        election_id = election_dir.name
        roster_path = election_dir / f"roster_ev_{election_id}.csv"
        if not roster_path.exists():
            continue
        meta = by_id.get(election_id)
        entries.append(
            _election_index_entry_from_roster(
                source_election_id=election_id,
                election_name=meta.election_name if meta else election_id,
                election_date=meta.election_date if meta else dt.date.today(),
                election_type=meta.election_type.value if meta else "unknown",
                certified=meta.certified if meta else None,
                roster_path=roster_path,
                source_prefix="civix",
                last_refreshed=_resolve_last_refreshed(
                    election_id,
                    roster_path,
                    existing_entries=existing_entries,
                    refreshed_ids=refreshed,
                ),
            )
        )
    return sorted(entries, key=lambda item: str(item["source_election_id"]))


def _build_legacy_index_entries(
    output_dir: Path,
    elections: list[object],
    *,
    index_path: Path | None = None,
    refreshed_ids: set[str] | None = None,
) -> list[dict[str, object]]:
    from .models import LegacyElection

    by_id = {e.source_election_id: e for e in elections if isinstance(e, LegacyElection)}
    existing_entries = _existing_index_entries(index_path)
    refreshed = refreshed_ids or set()
    entries: list[dict[str, object]] = []
    if not output_dir.exists():
        return entries

    for election_dir in sorted(output_dir.iterdir()):
        if not election_dir.is_dir():
            continue
        election_id = election_dir.name
        roster_path = election_dir / f"roster_ev_{election_id}.csv"
        if not roster_path.exists():
            continue
        meta = by_id.get(election_id)
        entries.append(
            _election_index_entry_from_roster(
                source_election_id=election_id,
                election_name=meta.election_name if meta else election_id,
                election_date=_legacy_election_date(meta),
                election_type=meta.election_type.value if meta else "unknown",
                certified=None,
                roster_path=roster_path,
                source_prefix="legacy",
                last_refreshed=_resolve_last_refreshed(
                    election_id,
                    roster_path,
                    existing_entries=existing_entries,
                    refreshed_ids=refreshed,
                ),
            )
        )
    return sorted(entries, key=lambda item: str(item["source_election_id"]))


def _write_election_index(
    index_path: Path,
    *,
    civix_entries: list[dict[str, object]],
    legacy_entries: list[dict[str, object]],
) -> bool:
    """Write index.json. Returns True when the file content changed."""
    index_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "last_updated": _iso_now(),
        "civix": {"elections": civix_entries},
        "legacy": {"elections": legacy_entries},
    }
    comparable = {"civix": payload["civix"], "legacy": payload["legacy"]}
    if index_path.exists():
        existing = json.loads(index_path.read_text())
        prior = {"civix": existing.get("civix"), "legacy": existing.get("legacy")}
        if prior == comparable:
            return False
    index_path.write_text(json.dumps(payload, indent=2) + "\n")
    return True


def _load_index_sections(
    index_path: Path,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    if not index_path.exists():
        return [], []
    data = json.loads(index_path.read_text())
    civix_entries = data.get("civix", {}).get("elections", [])
    legacy_entries = data.get("legacy", {}).get("elections", [])
    return civix_entries, legacy_entries


def _update_election_index(
    index_path: Path,
    *,
    civix_output_dir: Path | None = None,
    civix_elections: list[object] | None = None,
    legacy_output_dir: Path | None = None,
    legacy_elections: list[object] | None = None,
    refreshed_civix_ids: set[str] | None = None,
    refreshed_legacy_ids: set[str] | None = None,
) -> bool:
    civix_entries, legacy_entries = _load_index_sections(index_path)
    if civix_elections is not None and civix_output_dir is not None:
        civix_entries = _build_civix_index_entries(
            civix_output_dir,
            civix_elections,
            index_path=index_path,
            refreshed_ids=refreshed_civix_ids,
        )
    if legacy_elections is not None and legacy_output_dir is not None:
        legacy_entries = _build_legacy_index_entries(
            legacy_output_dir,
            legacy_elections,
            index_path=index_path,
            refreshed_ids=refreshed_legacy_ids,
        )
    return _write_election_index(
        index_path,
        civix_entries=civix_entries,
        legacy_entries=legacy_entries,
    )


def _run_typer_command(command: Callable[..., None], **kwargs: object) -> bool:
    """Run a Typer command function; return False on non-zero exit."""
    try:
        command(**kwargs)
        return True
    except typer.Exit as exc:
        return exc.exit_code == 0
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
        return code == 0


def _stdin_is_tty() -> bool:
    import sys

    return sys.stdin.isatty() and sys.stdout.isatty()


def _sort_civix_elections(elections: list[object]) -> list[object]:
    """Return Civix elections sorted by election_date descending (newest first)."""
    from .models import CivixElection

    civix_only = [e for e in elections if isinstance(e, CivixElection)]
    return sorted(
        civix_only,
        key=lambda e: (e.election_date, e.id),
        reverse=True,
    )


def _sort_civix_ev_dates(election: object) -> list[object]:
    """Return early_voting_dates sorted newest first."""
    from .models import CivixElection, CivixElectionDate

    if not isinstance(election, CivixElection):
        return []
    dates = [d for d in election.early_voting_dates if isinstance(d, CivixElectionDate)]
    return sorted(dates, key=lambda d: d.date, reverse=True)


def _format_civix_election_line(e: object, *, index: int | None = None) -> str:
    from .models import CivixElection

    if not isinstance(e, CivixElection):
        return ""
    cert = "yes" if e.certified else "no"
    prefix = f"{index:>3}. " if index is not None else ""
    return (
        f"{prefix}{e.id:>8}  {e.election_date!s:<12}  {e.election_type.value:<22}  "
        f"{cert:<5}  {len(e.early_voting_dates):>8}  {e.election_name}"
    )


def _echo_civix_elections_table(elections: list[object], *, numbered: bool = False) -> None:
    header = f"{'#':>3}  {'ID':>8}  {'DATE':<12}  {'TYPE':<22}  {'CERT':<5}  {'EV DATES':>8}  NAME"
    if not numbered:
        header = f"{'ID':>8}  {'DATE':<12}  {'TYPE':<22}  {'CERT':<5}  {'EV DATES':>8}  NAME"
    typer.echo(header)
    typer.echo("-" * len(header))
    for idx, election in enumerate(elections, start=1):
        typer.echo(_format_civix_election_line(election, index=idx if numbered else None))


def _prompt_select(message: str, options: list[tuple[str, str]]) -> str:
    """Arrow-key menu (questionary). Each option is ``(value, label)``."""
    import questionary
    from questionary import Choice

    if not options:
        typer.echo("Error: no choices available for prompt.", err=True)
        raise typer.Exit(code=1)

    selected = questionary.select(
        message,
        choices=[Choice(title=label, value=value) for value, label in options],
        use_arrow_keys=True,
        use_indicator=True,
        use_shortcuts=False,
    ).ask()

    if selected is None:
        raise typer.Exit(code=0)
    return selected


def _prompt_civix_election_id(elections: list[object]) -> str:
    from .models import CivixElection

    options: list[tuple[str, str]] = []
    for election in elections:
        if not isinstance(election, CivixElection):
            continue
        cert = "certified" if election.certified else "uncertified"
        label = (
            f"{election.id:>8}  {election.election_date}  "
            f"{election.election_type.value:<18}  {cert:<11}  {election.election_name}"
        )
        options.append((election.source_election_id, label))

    typer.echo("\nUse ↑/↓ and Enter to select an election (newest first).")
    return _prompt_select("Election", options)


def _prompt_civix_scrape_action() -> str:
    return _prompt_select(
        "What would you like to do?",
        [
            ("done", "Done (list only)"),
            ("turnout", "Fetch turnout for one EV date"),
            ("roster_per_county", "Fetch roster — per-county CSV (one EV date)"),
            ("roster_statewide", "Fetch roster — statewide bulk file (one EV date)"),
            ("fetch_all", "Fetch all EV dates (per-county, combined roster CSV)"),
        ],
    )


def _prompt_civix_ev_date(election: object) -> dt.date:
    ev_dates = _sort_civix_ev_dates(election)
    if not ev_dates:
        typer.echo("Error: election has no early voting dates.", err=True)
        raise typer.Exit(code=1)

    options = [(ev.date.isoformat(), str(ev.date)) for ev in ev_dates]
    typer.echo("\nUse ↑/↓ and Enter to select an early voting date (newest first).")
    picked = _prompt_select("EV date", options)
    return _parse_ev_date(picked)


def _run_civix_elections_interactive(elections: list[object]) -> None:
    """Prompt for election + scrape action, then dispatch to civix subcommands."""
    if not elections:
        typer.echo("No elections returned from Civix.", err=True)
        raise typer.Exit(code=1)

    election_id = _prompt_civix_election_id(elections)
    from .models import CivixElection

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
                "Prompt to select an election and scrape action "
                "(default: on when stdout is a TTY)"
            ),
        ),
    ] = None,
) -> None:
    """List Civix elections (newest first) and optionally run an interactive scrape wizard."""
    from .civix import CivixClient
    from .models import CivixElection

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
    from .civix import CivixClient

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
    from .civix import CivixClient, fetch_county_roster

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

    from .audit import audit_records
    from .civix import CivixClient, fetch_county_roster
    from .http_transport import HTTP_FETCH_EXCEPTIONS, format_fetch_error
    from .models import CountyRoster, CountyTurnout, VoterRecord
    from .writer import (
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
                    from .civix import parse_ed_statewide_voter_records

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
                                (
                                    row.county_id
                                    for row in ed_turnout_rows
                                    if row.county == county
                                ),
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
    from .models import TurnoutRosterGapReport
    from .terminal_report import print_gap_report_summary

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
    from .gap_analysis import (
        stored_gap_counties_csv_path,
        stored_gap_report_path,
        try_build_civix_gap_report,
        write_gap_counties_csv,
        write_gap_report_json,
    )
    from .writer import read_roster_csv

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
    from .civix import CivixClient

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


# ---------------------------------------------------------------------------
# Legacy subcommands
# ---------------------------------------------------------------------------


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
    source_election_id: Annotated[
        str, typer.Argument(help="Legacy SOS election ID string (e.g. '49664')")
    ],
    ev_date: EvDateStr,
    output: Annotated[
        str, typer.Option("--output", "-o", help="Output format: 'table' or 'json'")
    ] = "table",
) -> None:
    """Fetch county EV turnout from the legacy SOS portal."""
    from . import legacy_api

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

    from . import legacy_api

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

    from . import legacy_api
    from .audit import audit_records
    from .http_transport import HTTP_FETCH_EXCEPTIONS, format_fetch_error
    from .models import CountyRoster
    from .roster import fetch_roster_strategy_a
    from .session import LegacySession
    from .turnout import extract_county_ids, fetch_ev_details_html
    from .writer import accumulate_roster, load_stored_turnout_for_audit, write_roster_csv

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
    from . import legacy_api

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


# ---------------------------------------------------------------------------
# Audit subcommands (root-level, not namespaced under civix/legacy)
# ---------------------------------------------------------------------------


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
    from .audit import audit_records
    from .writer import (
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
    from .audit import audit_from_csv

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
            inferred_date = dt.datetime.strptime(part, "%Y-%m-%d").date()
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


# ---------------------------------------------------------------------------
# Voterfile subcommands
# ---------------------------------------------------------------------------

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
    from .voterfile import (
        detect_columns,
        list_voterfile_columns,
        load_mapping,
        mapping_column_conflicts,
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

            from .models import ColumnMapping as _CM2

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
        from .gap_analysis import (
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
    from .terminal_report import print_voterfile_match_summary

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
