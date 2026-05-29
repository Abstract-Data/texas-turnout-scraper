"""Voter roster fetching for the legacy Texas SOS early-voting portal.

Two strategies are provided:

**Strategy A (default)** — per-county loop
    Issues one POST to ``downloadVoterInfoReport.do`` per county (~255 requests for a
    statewide election).  Each response is raw CSV.  Paced at ≥1.0 s between
    requests to match the legacy ingest convention.

**Strategy B** — bulk ZIP download
    Downloads a single large ZIP (~35 MB) that contains all county rosters.
    The ZIP is streamed directly to disk and **never** buffered in memory.
    Unreliable for large elections due to portal timeouts.

PII constraints (Texas Election Code):
    ``VOTER_NAME`` is parsed only to be discarded.
    ``ID_VOTER`` (Texas VUID) is always kept as a string — never coerced to int.
    Neither value is ever written to a log.
"""

from __future__ import annotations

import csv
import io
import logging
import time
from contextlib import nullcontext
from datetime import date
from pathlib import Path

import httpx
from requests.exceptions import HTTPError as RequestsHTTPError
from requests.exceptions import RequestException

from .legacy_forms import legacy_ev_form_fields
from .models import CountyRoster, VoterRecord
from .session import LegacySession

logger = logging.getLogger(__name__)

_EV_REPORT_PATH = "/Elections/downloadVoterInfoReport.do"
_BULK_DOWNLOAD_PATH = "/Elections/downloadParticipationCountReport.do"


# ---------------------------------------------------------------------------
# Strategy A — per-county loop
# ---------------------------------------------------------------------------


def fetch_roster_strategy_a(
    session: LegacySession,
    source_election_id: str,
    ev_date: date,
    county_ids: list[str],
    pace_seconds: float = 1.0,
    county_names: dict[str, str] | None = None,
    *,
    skip_prime: bool = False,
) -> list[CountyRoster]:
    """Fetch voter rosters one county at a time via ``downloadVoterInfoReport.do``.

    Issues one ``POST`` per county (typically ~255 for a statewide election),
    paced at *pace_seconds* between requests.  Each response is raw CSV with
    the header::

        "VOTER_NAME","ID_VOTER","VOTING_METHOD","PRECINCT"

    ``VOTER_NAME`` is stored on :class:`VoterRecord` for name-mismatch detection
    only — it is never logged.  ``ID_VOTER`` is kept as a plain string (10-digit
    Texas VUID — leading zeros must be preserved).

    Args:
        session: An established :class:`~texas_turnout_scraper.session.LegacySession`.
        source_election_id: The SOS numeric ID string (e.g. ``"49664"``).
            Never coerced to int.
        ev_date: The early-voting date to fetch rosters for.
        county_ids: Ordered list of county ``townId`` strings (not poll-place
            select values), typically extracted from the turnout page via
            :func:`~texas_turnout_scraper.turnout.extract_county_ids`.
        pace_seconds: Minimum seconds between county requests. Applied via
            :meth:`~texas_turnout_scraper.session.LegacySession.with_pace` for
            the duration of this fetch.
        county_names: Optional mapping of county_id -> county name (all-caps).
            When provided, the resolved name is used on each VoterRecord.
            When absent, falls back to ``"COUNTY_{county_id}"``.
        skip_prime: When ``True``, skip
            :meth:`~texas_turnout_scraper.session.LegacySession.prime_election`
            because the session was already primed (e.g. via
            :func:`~texas_turnout_scraper.turnout.fetch_ev_details_html`).

    Returns:
        A list of :class:`~texas_turnout_scraper.models.CountyRoster` objects,
        one per county.

    Raises:
        RuntimeError: If any county request fails, returns an empty body, or
            yields CSV that cannot be parsed.

    Note:
        Neither ``ID_VOTER`` nor ``VOTER_NAME`` values are written to logs at
        any severity level.
    """
    date_str = ev_date.strftime("%Y-%m-%d")
    rosters: list[CountyRoster] = []
    failed_counties: list[str] = []

    if not skip_prime:
        session.prime_election(source_election_id)

    pace_ctx = session.with_pace(pace_seconds) if pace_seconds > 0 else nullcontext()

    with pace_ctx:
        for county_id in county_ids:
            try:
                resp = session.post_form(
                    _EV_REPORT_PATH,
                    legacy_ev_form_fields(
                        source_election_id,
                        ev_date,
                        id_town=county_id,
                    ),
                )
            except (httpx.HTTPStatusError, RequestsHTTPError, RequestException):
                logger.error(
                    "Request failed for county_id=%s on %s.",
                    county_id,
                    date_str,
                )
                failed_counties.append(county_id)
                continue

            raw_text = resp.text.strip()
            if not raw_text:
                logger.warning("Empty response for county_id=%s on %s.", county_id, date_str)
                failed_counties.append(county_id)
                continue

            county_name = (county_names or {}).get(county_id, f"COUNTY_{county_id}")
            roster = _parse_county_csv(
                raw_text=raw_text,
                county_id=county_id,
                county_name=county_name,
                source_election_id=source_election_id,
                ev_date=ev_date,
            )
            if roster is None:
                failed_counties.append(county_id)
                continue

            rosters.append(roster)

    if failed_counties:
        msg = (
            f"{len(failed_counties)} of {len(county_ids)} counties failed "
            f"for election {source_election_id} on {date_str}"
        )
        raise RuntimeError(msg)

    logger.info(
        "Strategy A: fetched %d county rosters for election %s on %s.",
        len(rosters),
        source_election_id,
        date_str,
    )
    return rosters


# ---------------------------------------------------------------------------
# Strategy B — bulk ZIP download
# ---------------------------------------------------------------------------


def fetch_roster_strategy_b(
    session: LegacySession,
    source_election_id: str,
    ev_date: date,
    output_dir: Path,
) -> Path:
    """Download the bulk roster ZIP for an election date, streaming to disk.

    The ZIP file (~35 MB) is written to *output_dir* in streaming chunks and
    is **never** buffered in memory.  This avoids OOM conditions for large
    elections.

    Args:
        session: An established :class:`~texas_turnout_scraper.session.LegacySession`.
        source_election_id: The SOS numeric ID string (e.g. ``"49664"``).
        ev_date: The early-voting date for which to download the bulk roster.
        output_dir: Directory to write the downloaded ZIP into.  Created if it
            does not exist.

    Returns:
        The :class:`pathlib.Path` of the saved ``.zip`` file.

    Raises:
        httpx.HTTPStatusError: If ``http_backend="httpx"`` and the portal returns
            a non-2xx status.
        requests.HTTPError: If ``http_backend="cloudscraper"`` (default) and the
            portal returns a non-2xx status.
        OSError: If the output directory cannot be created or the file cannot
            be written.

    Note:
        Strategy B is unreliable for large elections due to portal-side
        timeouts.  Prefer Strategy A for production use.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    date_str = ev_date.strftime("%Y-%m-%d")
    zip_path = output_dir / f"roster_{date_str}_bulk.zip"

    session.prime_election(source_election_id)

    # Pace before the request
    with session.stream(
        "POST",
        _BULK_DOWNLOAD_PATH,
        data=legacy_ev_form_fields(source_election_id, ev_date),
    ) as resp:
        resp.raise_for_status()
        with zip_path.open("wb") as fh:
            for chunk in resp.iter_bytes(chunk_size=65_536):
                fh.write(chunk)

    session._last_request_at = time.monotonic()

    logger.info(
        "Strategy B: saved bulk ZIP to %s (%d bytes).",
        zip_path,
        zip_path.stat().st_size,
    )
    return zip_path


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_county_csv(
    raw_text: str,
    county_id: str,
    county_name: str,
    source_election_id: str,
    ev_date: date,
) -> CountyRoster | None:
    """Parse a raw CSV response body into a :class:`CountyRoster`.

    The SOS portal returns CSV with a header row::

        "VOTER_NAME","ID_VOTER","VOTING_METHOD","PRECINCT"

    ``VOTER_NAME`` is stored on each :class:`VoterRecord` for name-mismatch
    detection by ``accumulate_roster()`` — it is never logged.
    ``ID_VOTER`` is kept as a plain string at all times.

    Args:
        raw_text: Raw CSV text from the portal response.
        county_id: The county ID string used in the POST (used only to
            construct :class:`CountyRoster` when county_name is a fallback).
        county_name: Resolved county name (all-caps). Falls back to
            ``"COUNTY_{county_id}"`` when the caller cannot resolve it.
        source_election_id: The SOS election ID string.
        ev_date: The early-voting date.

    Returns:
        A :class:`CountyRoster`, or ``None`` if the CSV cannot be parsed or
        has no data rows.
    """
    try:
        reader = csv.DictReader(io.StringIO(raw_text), quotechar='"')
        records: list[VoterRecord] = []

        for row in reader:
            row = {k.strip().strip('"'): v.strip().strip('"') for k, v in row.items() if k}
            if not row.get("ID_VOTER", "").strip():
                continue
            records.append(
                VoterRecord.from_csv_row(
                    row,
                    county=county_name,
                    election_id=source_election_id,
                    report_date=ev_date,
                )
            )

        if not records:
            logger.debug("No voter records parsed for county_id=%s.", county_id)
            return None

        return CountyRoster(
            county=county_name,
            county_id=None,  # Legacy portal does not expose a numeric county_id
            election_id=source_election_id,
            report_date=ev_date,
            source="legacy",
            records=records,
        )

    except (csv.Error, ValueError, KeyError) as exc:
        logger.warning(
            "Failed to parse CSV for county_id=%s (election %s, date %s) — %s.",
            county_id,
            source_election_id,
            ev_date,
            type(exc).__name__,
        )
        return None
