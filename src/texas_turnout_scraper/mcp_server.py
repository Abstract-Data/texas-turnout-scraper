"""FastMCP server exposing texas-turnout-scraper tools to AI agents.

All tools that return roster data return ONLY SUMMARY COUNTS — never
individual voter records or VUID values.

Source modules are imported lazily inside each tool function to keep
MCP server startup fast.

Date parameters come in as str from MCP callers and are parsed internally
via datetime.strptime(date_str, "%Y-%m-%d").date().
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

mcp = FastMCP(
    "texas-turnout",
    instructions=(
        "Texas SOS early-voting turnout data tools. "
        "Civix tools cover 2025+ elections via the EVR API; "
        "Legacy tools cover pre-2025 elections via the SOS HTML portal."
    ),
)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _parse_date(date_str: str):
    """Parse a YYYY-MM-DD string into a datetime.date."""
    from datetime import datetime

    return datetime.strptime(date_str, "%Y-%m-%d").date()


# ---------------------------------------------------------------------------
# Civix tools (2025+)
# ---------------------------------------------------------------------------


@mcp.tool()
def civix_list_elections() -> list[dict]:
    """List all elections in the Civix EVR system (2025+).

    Returns a list of election dicts, each containing:
      - id (int): raw Civix integer election ID
      - source_election_id (str): canonical string key
      - name (str): human-readable election name
      - election_date (str): YYYY-MM-DD
      - election_type (str): inferred type (e.g. 'primary', 'general')
      - certified (bool): whether results are certified
      - ev_dates_count (int): number of early-voting dates available
      - counties_count (int): number of participating counties
    """
    from .civix import CivixClient

    with CivixClient() as client:
        elections = client.list_elections()

    return [
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


@mcp.tool()
def civix_fetch_turnout(election_id: int, election_date: str) -> list[dict]:
    """Fetch county early-voting (EV) turnout for a Civix election.

    Args:
        election_id: Civix integer election ID (from civix_list_elections).
        election_date: EV date in YYYY-MM-DD format.

    Returns a list of county turnout dicts, each containing:
      - county (str): county name in ALL CAPS
      - county_id (int): Civix county ID
      - registered_voters (int)
      - in_person_votes_on_date (int): votes cast on this specific date
      - total_in_person_votes (int): cumulative in-person votes through this date
      - total_mail_votes (int): cumulative mail-in votes through this date
      - roster_available (bool): whether a voter roster is available
      - source (str): 'civix'
    """
    from .civix import CivixClient

    ev_date = _parse_date(election_date)
    with CivixClient() as client:
        rows = client.fetch_ev_turnout(election_id=election_id, election_date=ev_date)

    return [
        {
            "county": r.county,
            "county_id": r.county_id,
            "registered_voters": r.registered_voters,
            "in_person_votes_on_date": r.in_person_votes_on_date,
            "total_in_person_votes": r.total_in_person_votes,
            "total_mail_votes": r.total_mail_votes,
            "roster_available": r.roster_available,
            "source": r.source,
        }
        for r in rows
    ]


@mcp.tool()
def civix_fetch_county_roster(
    election_id: int,
    election_date: str,
    county_name: str,
    county_id: int,
) -> dict:
    """Fetch the EV voter roster for one county from the Civix system.

    Returns summary counts only — NEVER returns individual VUID values or voter names.

    Args:
        election_id: Civix integer election ID.
        election_date: EV date in YYYY-MM-DD format.
        county_name: County name in ALL CAPS (e.g. 'HARRIS').
        county_id: Civix county ID (from civix_fetch_turnout or civix_list_elections).

    Returns a dict containing:
      - county (str): county name
      - county_id (int): Civix county ID
      - election_id (str): source_election_id string
      - report_date (str): YYYY-MM-DD
      - total_voters (int): total voters in the roster
      - in_person (int): number of in-person voters
      - mail_in (int): number of mail-in voters
      - source (str): 'civix'
    """
    from .civix import CivixClient, fetch_county_roster

    ev_date = _parse_date(election_date)
    with CivixClient() as client:
        roster = fetch_county_roster(
            client,
            election_id=election_id,
            election_date=ev_date,
            county_name=county_name,
            county_id=county_id,
        )

    return {
        "county": roster.county,
        "county_id": roster.county_id,
        "election_id": roster.election_id,
        "report_date": str(roster.report_date),
        "total_voters": roster.total_voters,
        "in_person": roster.in_person_count,
        "mail_in": roster.mail_in_count,
        "source": roster.source,
    }


@mcp.tool()
def civix_fetch_ed_turnout(election_id: int, election_date: str) -> list[dict]:
    """Fetch election day county turnout for a Civix election.

    Args:
        election_id: Civix integer election ID.
        election_date: Election day date in YYYY-MM-DD format.

    Returns a list of county turnout dicts with the same structure as
    civix_fetch_turnout but sourced from the election day turnout endpoint.
    Each dict contains:
      - county (str)
      - county_id (int)
      - registered_voters (int)
      - in_person_votes_on_date (int)
      - total_in_person_votes (int)
      - total_mail_votes (int)
      - roster_available (bool)
      - source (str): 'civix'
    """
    from .civix import CivixClient

    ed_date = _parse_date(election_date)
    with CivixClient() as client:
        rows = client.fetch_ed_turnout(election_id=election_id, election_date=ed_date)

    return [
        {
            "county": r.county,
            "county_id": r.county_id,
            "registered_voters": r.registered_voters,
            "in_person_votes_on_date": r.in_person_votes_on_date,
            "total_in_person_votes": r.total_in_person_votes,
            "total_mail_votes": r.total_mail_votes,
            "roster_available": r.roster_available,
            "source": r.source,
        }
        for r in rows
    ]


@mcp.tool()
def civix_fetch_polling_places(
    election_id: int,
    county_name: str = "STATEWIDE_POLLING_PLACE_INFO",
) -> str:
    """Fetch polling place information for a Civix election.

    Args:
        election_id: Civix integer election ID.
        county_name: County name in ALL CAPS, or 'STATEWIDE_POLLING_PLACE_INFO'
                     (default) to retrieve polling place info for all counties.

    Returns the raw CSV text of polling place information.
    """
    from .civix import CivixClient

    with CivixClient() as client:
        csv_text = client.fetch_polling_places(
            election_id=election_id,
            name=county_name,
        )

    return csv_text


# ---------------------------------------------------------------------------
# Legacy tools (pre-2025 SOS HTML portal)
# ---------------------------------------------------------------------------


@mcp.tool()
def legacy_list_elections() -> list[dict]:
    """List all elections from the legacy SOS HTML portal (pre-2025).

    Returns a list of election dicts, each containing:
      - source_election_id (str): canonical string key (e.g. '49664')
      - name (str): human-readable election name
      - election_type (str): inferred type (e.g. 'primary', 'general')
      - election_year (int | None): year of the election

    Early-voting date counts are not included here (listing does not fetch
    per-election EV calendars). Use legacy portal workflows or CLI helpers
    to resolve dates for a specific ``source_election_id``.
    """
    from . import legacy_api

    elections = legacy_api.list_elections()

    return [
        {
            "source_election_id": e.source_election_id,
            "name": e.election_name,
            "election_type": e.election_type.value,
            "election_year": e.election_year,
        }
        for e in elections
    ]


@mcp.tool()
def legacy_fetch_turnout(source_election_id: str, ev_date: str) -> list[dict]:
    """Fetch county EV turnout from the legacy SOS HTML portal.

    Args:
        source_election_id: Legacy SOS election ID string (e.g. '49664').
                            Always a string — never coerce to int.
        ev_date: EV date in YYYY-MM-DD format.

    Returns a list of county turnout dicts, each containing:
      - election_id (str)
      - report_date (str): YYYY-MM-DD
      - county (str)
      - registered_voters (int)
      - in_person_votes_on_date (int)
      - total_in_person_votes (int)
      - total_mail_votes (int)
      - roster_available (bool)
      - source (str): 'legacy'
    """
    from . import legacy_api

    report_date = _parse_date(ev_date)
    rows = legacy_api.fetch_county_turnout(
        source_election_id=source_election_id,
        ev_date=report_date,
    )

    return [
        {
            "election_id": r.election_id,
            "report_date": str(r.report_date),
            "county": r.county,
            "registered_voters": r.registered_voters,
            "in_person_votes_on_date": r.in_person_votes_on_date,
            "total_in_person_votes": r.total_in_person_votes,
            "total_mail_votes": r.total_mail_votes,
            "roster_available": r.roster_available,
            "source": r.source,
        }
        for r in rows
    ]


@mcp.tool()
def legacy_fetch_county_roster(
    source_election_id: str,
    ev_date: str,
    county_id: str,
) -> dict:
    """Fetch the EV voter roster for one county from the legacy SOS portal.

    Returns summary counts only — NEVER returns individual VUID values or voter names.

    Args:
        source_election_id: Legacy SOS election ID string (e.g. '49664').
                            Always a string — never coerce to int.
        ev_date: EV date in YYYY-MM-DD format.
        county_id: County identifier string as used by the legacy SOS portal.

    Returns a dict containing:
      - county (str): county name
      - election_id (str): source_election_id
      - report_date (str): YYYY-MM-DD
      - total_voters (int): total voters in the roster
      - in_person (int): number of in-person voters
      - mail_in (int): number of mail-in voters
      - source (str): 'legacy'
    """
    from . import legacy_api

    report_date = _parse_date(ev_date)
    roster = legacy_api.fetch_single_county_roster(
        source_election_id=source_election_id,
        ev_date=report_date,
        county_id=county_id,
    )

    return {
        "county": roster.county,
        "election_id": roster.election_id,
        "report_date": str(roster.report_date),
        "total_voters": roster.total_voters,
        "in_person": roster.in_person_count,
        "mail_in": roster.mail_in_count,
        "source": roster.source,
    }


# ---------------------------------------------------------------------------
# Shared audit tool
# ---------------------------------------------------------------------------


@mcp.tool()
def run_audit(
    election_id: str,
    ev_date: str,
    source: str = "civix",
    data_dir: str = "data",
) -> dict:
    """Run a data quality audit on a stored roster file.

    Checks for:
      - Duplicate VUIDs within a roster
      - VUIDs appearing with both IN-PERSON and MAIL-IN methods (cross-method duplicates)
      - County turnout exceeding registered voter count
      - Missing counties

    Args:
        election_id: Election ID string (source_election_id).
        ev_date: EV date in YYYY-MM-DD format.
        source: Data source — 'civix' or 'legacy'. Determines roster file path.
        data_dir: Root data directory (default: 'data').

    Returns a dict containing the full AuditReport:
      - election_id (str)
      - report_date (str): YYYY-MM-DD
      - source (str)
      - total_records (int)
      - unique_vuids (int)
      - duplicate_vuid_count (int)
      - cross_method_duplicate_count (int)
      - findings (list[dict]): list of individual findings with finding_type, county, detail, severity
      - generated_at (str): ISO 8601 timestamp
    """
    from pathlib import Path

    from .audit import audit_records
    from .writer import (
        load_stored_turnout_for_audit,
        read_roster_csv,
        report_date_from_roster_csv,
        stored_roster_ev_path,
    )

    source_key = source.lower()
    if source_key not in {"civix", "legacy"}:
        return {
            "error": f"Invalid source {source!r}; expected 'civix' or 'legacy'.",
            "election_id": election_id,
            "ev_date": ev_date,
            "source": source,
        }

    roster_path = stored_roster_ev_path(Path(data_dir), source_key, election_id)

    if not roster_path.exists():
        return {
            "error": f"Roster file not found: {roster_path}",
            "election_id": election_id,
            "ev_date": ev_date,
            "source": source,
        }

    report_date = (
        _parse_date(ev_date) if ev_date.strip() else report_date_from_roster_csv(roster_path)
    )

    records = read_roster_csv(roster_path)
    turnout = load_stored_turnout_for_audit(
        Path(data_dir),
        source_key,
        election_id,
        report_dates={r.report_date for r in records} or None,
    )
    report = audit_records(
        records,
        turnout=turnout,
        election_id=election_id,
        report_date=report_date,
        source=source_key,
    )

    return report.model_dump(mode="json")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
