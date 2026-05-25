"""Shared Struts form fields for the legacy SOS early-voting portal."""

from __future__ import annotations

from datetime import date


def legacy_ev_form_fields(
    source_election_id: str,
    ev_date: date,
    *,
    id_town: str = "",
) -> dict[str, str]:
    """Build form body fields for EV report POSTs (turnout, roster, bulk).

    Matches ``docs/EARLY_VOTING_ROSTER.md`` — ``selectedDate`` in
    ``YYYY-MM-DD HH:MM:SS.0`` format, ``idTown`` empty for turnout/bulk or set
    to the county ``townId`` for per-county roster CSV.
    """
    date_str = ev_date.strftime("%Y-%m-%d")
    struts_date = f"{date_str} 00:00:00.0"
    return {
        "idElection": source_election_id,
        "selectedDate": struts_date,
        "electionDate": "",
        "earlyVoteFlag": "true",
        "downloadElectionFileCSVFlag": "false",
        "idTown": id_town,
    }
