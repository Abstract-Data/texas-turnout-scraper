"""Turnout vs roster gap analysis.

Compares published Civix cumulative turnout (what SOS posts online) against
unique voters present in scraped per-county roster CSVs.
"""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from datetime import date
from pathlib import Path

from .enums import VoteMethod
from .models import CountyTurnout, CountyTurnoutRosterGap, TurnoutRosterGapReport, VoterRecord
from .writer import read_turnout_csv


def summarize_roster_by_county(
    records: list[VoterRecord],
) -> dict[str, tuple[int, int, int]]:
    """Return per-county ``(in_person_unique, mail_only_unique, total_unique)`` counts."""
    in_person: dict[str, set[str]] = defaultdict(set)
    mail: dict[str, set[str]] = defaultdict(set)

    for rec in records:
        county = rec.county
        if rec.voting_method is VoteMethod.IN_PERSON:
            in_person[county].add(rec.id_voter)
        elif rec.voting_method is VoteMethod.MAIL_IN:
            mail[county].add(rec.id_voter)

    counties = sorted(set(in_person) | set(mail))
    summary: dict[str, tuple[int, int, int]] = {}
    for county in counties:
        ip_vuids = in_person[county]
        mail_only = mail[county] - ip_vuids
        total = ip_vuids | mail[county]
        summary[county] = (len(ip_vuids), len(mail_only), len(total))
    return summary


def build_turnout_roster_gap_report(  # noqa: PLR0913
    *,
    election_id: str,
    ev_date: date,
    roster_path: Path,
    roster_records: list[VoterRecord],
    turnout_rows: list[CountyTurnout],
    election_name: str | None = None,
    certified: bool | None = None,
    source: str = "civix",
    turnout_source: str = "live",
) -> TurnoutRosterGapReport:
    """Build a county-level gap report from turnout summaries and roster records."""
    roster_by_county = summarize_roster_by_county(roster_records)
    turnout_by_county = {row.county: row for row in turnout_rows}
    all_counties = sorted(set(roster_by_county) | set(turnout_by_county))

    county_rows: list[CountyTurnoutRosterGap] = []
    totals = {
        "turnout_in_person": 0,
        "turnout_mail": 0,
        "turnout_total": 0,
        "roster_in_person": 0,
        "roster_mail": 0,
        "roster_total": 0,
        "gap_in_person": 0,
        "gap_mail": 0,
        "gap_total": 0,
    }
    counties_with_gap = 0
    counties_roster_over_turnout = 0

    for county in all_counties:
        turnout = turnout_by_county.get(county)
        roster_ip, roster_mail, roster_total = roster_by_county.get(county, (0, 0, 0))

        turnout_ip = turnout.total_in_person_votes if turnout else 0
        turnout_mail = turnout.total_mail_votes if turnout else 0
        turnout_total = turnout_ip + turnout_mail

        gap_ip = turnout_ip - roster_ip
        gap_mail = turnout_mail - roster_mail
        gap_total = turnout_total - roster_total
        gap_pct = (gap_total / turnout_total) if turnout_total else 0.0

        if gap_total > 0:
            counties_with_gap += 1
        elif gap_total < 0:
            counties_roster_over_turnout += 1

        county_rows.append(
            CountyTurnoutRosterGap(
                county=county,
                county_id=turnout.county_id if turnout else None,
                registered_voters=turnout.registered_voters if turnout else 0,
                turnout_in_person=turnout_ip,
                turnout_mail=turnout_mail,
                turnout_total=turnout_total,
                roster_in_person=roster_ip,
                roster_mail=roster_mail,
                roster_total=roster_total,
                gap_in_person=gap_ip,
                gap_mail=gap_mail,
                gap_total=gap_total,
                gap_pct=gap_pct,
            )
        )

        totals["turnout_in_person"] += turnout_ip
        totals["turnout_mail"] += turnout_mail
        totals["turnout_total"] += turnout_total
        totals["roster_in_person"] += roster_ip
        totals["roster_mail"] += roster_mail
        totals["roster_total"] += roster_total
        totals["gap_in_person"] += gap_ip
        totals["gap_mail"] += gap_mail
        totals["gap_total"] += gap_total

    unique_vuids = len({rec.id_voter for rec in roster_records})
    gap_pct = totals["gap_total"] / totals["turnout_total"] if totals["turnout_total"] else 0.0

    return TurnoutRosterGapReport(
        election_id=election_id,
        election_name=election_name,
        certified=certified,
        source=source,
        ev_date=ev_date,
        roster_path=str(roster_path),
        turnout_source=turnout_source,
        roster_row_count=len(roster_records),
        roster_unique_vuids=unique_vuids,
        counties=county_rows,
        turnout_in_person=totals["turnout_in_person"],
        turnout_mail=totals["turnout_mail"],
        turnout_total=totals["turnout_total"],
        roster_in_person=totals["roster_in_person"],
        roster_mail=totals["roster_mail"],
        roster_total=totals["roster_total"],
        gap_in_person=totals["gap_in_person"],
        gap_mail=totals["gap_mail"],
        gap_total=totals["gap_total"],
        gap_pct=gap_pct,
        counties_with_gap=counties_with_gap,
        counties_roster_over_turnout=counties_roster_over_turnout,
    )


def infer_civix_election_from_roster(
    roster_path: Path,
    roster_records: list[VoterRecord],
) -> tuple[str, Path] | None:
    """Return ``(election_id, election_dir)`` when the roster looks like Civix fetch-all output."""
    if roster_path.name.startswith("roster_ev_") and roster_path.name.endswith(".csv"):
        election_id = roster_path.stem.removeprefix("roster_ev_")
        if election_id.isdigit():
            return election_id, roster_path.parent

    parts = roster_path.parts
    if "civix" in parts:
        idx = parts.index("civix")
        if idx + 1 < len(parts) and parts[idx + 1].isdigit():
            return parts[idx + 1], roster_path.parent

    return None


def load_civix_turnout_rows(
    *,
    election_dir: Path,
    civix_id: int,
    ev_date: date,
    turnout_source: str,
    client: object | None = None,
) -> tuple[list[CountyTurnout], str]:
    """Load turnout rows from disk or live Civix API."""
    from .civix import CivixClient

    stored_path = election_dir / f"turnout_ev_{ev_date.isoformat()}.csv"
    use_stored = turnout_source in {"stored", "auto"} and stored_path.exists()
    if use_stored:
        return read_turnout_csv(stored_path), "stored"

    if turnout_source == "stored":
        raise FileNotFoundError(f"stored turnout not found: {stored_path}")

    civix_client = client if isinstance(client, CivixClient) else CivixClient()
    owns_client = not isinstance(client, CivixClient)
    try:
        rows = civix_client.fetch_ev_turnout(election_id=civix_id, election_date=ev_date)
        return [CountyTurnout(**row.model_dump()) for row in rows], "live"
    finally:
        if owns_client:
            civix_client.close()


def try_build_civix_gap_report(
    *,
    roster_path: Path,
    roster_records: list[VoterRecord],
    ev_date: date | None = None,
    turnout_source: str = "auto",
) -> TurnoutRosterGapReport | None:
    """Build a gap report when the roster is a Civix combined EV file."""
    import logging

    from .civix import CivixClient

    logger = logging.getLogger(__name__)
    inferred = infer_civix_election_from_roster(roster_path, roster_records)
    if inferred is None:
        return None

    election_id, election_dir = inferred
    if not roster_records:
        return None

    parsed_ev_date = ev_date or max(rec.report_date for rec in roster_records)

    try:
        with CivixClient() as client:
            elections = client.list_elections()
            election = next(
                (
                    item
                    for item in elections
                    if str(item.id) == election_id or item.source_election_id == election_id
                ),
                None,
            )
            if election is None:
                return None
            turnout_rows, resolved_source = load_civix_turnout_rows(
                election_dir=election_dir,
                civix_id=election.id,
                ev_date=parsed_ev_date,
                turnout_source=turnout_source,
                client=client,
            )
    except (OSError, ValueError, FileNotFoundError) as exc:
        logger.warning("Skipping turnout vs roster gap report: %s", exc)
        return None
    except Exception as exc:
        from .http_transport import HTTP_FETCH_EXCEPTIONS

        if isinstance(exc, HTTP_FETCH_EXCEPTIONS):
            logger.warning("Skipping turnout vs roster gap report: %s", exc)
            return None
        raise

    return build_turnout_roster_gap_report(
        election_id=election_id,
        ev_date=parsed_ev_date,
        roster_path=roster_path,
        roster_records=roster_records,
        turnout_rows=turnout_rows,
        election_name=election.election_name,
        certified=election.certified,
        source="civix",
        turnout_source=resolved_source,
    )


def stored_gap_report_path(data_dir: Path, source: str, election_id: str) -> Path:
    """Default JSON path for a gap report under ``data/elections/{source}/{id}/``."""
    return data_dir / "elections" / source / election_id / f"gap_report_ev_{election_id}.json"


def stored_gap_counties_csv_path(data_dir: Path, source: str, election_id: str) -> Path:
    """Default county-level CSV path for a gap report."""
    return data_dir / "elections" / source / election_id / f"gap_counties_ev_{election_id}.csv"


def write_gap_report_json(report: TurnoutRosterGapReport, path: Path) -> Path:
    """Write a gap report to JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report.model_dump(mode="json"), indent=2), encoding="utf-8")
    return path


def write_gap_counties_csv(report: TurnoutRosterGapReport, path: Path) -> Path:
    """Write per-county gap rows to CSV for spreadsheet analysis."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(CountyTurnoutRosterGap.model_fields.keys())
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in report.counties:
            writer.writerow(row.model_dump())
    return path
