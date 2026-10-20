"""Shared CLI helpers — index management, parsing, civix sorting."""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Callable
from pathlib import Path

import typer

from ._typer_apps import _FRESHNESS_HOURS


def _utc_today() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()


def _parse_ev_date(value: str | dt.date) -> dt.date:
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise typer.BadParameter(
            f"Invalid EV date {value!r}; expected YYYY-MM-DD.",
        ) from exc


def _resolve_civix_election(elections: list[object], election_id: str) -> object | None:
    from ..models import CivixElection

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
    from ..audit import audit_records
    from ..writer import read_roster_csv

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
    from ..models import LegacyElection

    if isinstance(meta, LegacyElection):
        if meta.ev_dates:
            return meta.ev_dates[-1].date
        if meta.election_year is not None:
            return dt.date(meta.election_year, 11, 1)
    return _utc_today()


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
    from ..models import CivixElection

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
                election_date=meta.election_date if meta else _utc_today(),
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
    from ..models import LegacyElection

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
    from ..models import CivixElection

    civix_only = [e for e in elections if isinstance(e, CivixElection)]
    return sorted(
        civix_only,
        key=lambda e: (e.election_date, e.id),
        reverse=True,
    )


def _sort_civix_ev_dates(election: object) -> list[object]:
    """Return early_voting_dates sorted newest first."""
    from ..models import CivixElection, CivixElectionDate

    if not isinstance(election, CivixElection):
        return []
    dates = [d for d in election.early_voting_dates if isinstance(d, CivixElectionDate)]
    return sorted(dates, key=lambda d: d.date, reverse=True)


def _format_civix_election_line(e: object, *, index: int | None = None) -> str:
    from ..models import CivixElection

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
    from ..models import CivixElection

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
