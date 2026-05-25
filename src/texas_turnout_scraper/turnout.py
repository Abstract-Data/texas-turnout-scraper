"""County turnout table scraping for the legacy Texas SOS early-voting portal.

Public API:

* :func:`fetch_ev_details_html` — Session step 2 + 3: prime election, POST
  ``getEVDetails.do``, return raw HTML (shared by turnout and roster facades).

* :func:`fetch_turnout` — Parses the county turnout table into
  :class:`~texas_turnout_scraper.models.CountyTurnout` rows.

* :func:`extract_county_ids` — County name → ``townId`` mapping from turnout HTML
  for Strategy A roster fetching (see :mod:`~texas_turnout_scraper.roster`).

Uses :class:`~texas_turnout_scraper.session.LegacySession` (cloudscraper default;
``http_backend="httpx"`` in unit tests). No Selenium. No election_utils.
"""

from __future__ import annotations

import logging
import re
from datetime import date

from bs4 import BeautifulSoup, Tag

from .legacy_forms import legacy_ev_form_fields
from .models import CountyTurnout
from .session import LegacySession

logger = logging.getLogger(__name__)

_EV_DETAILS_PATH = "/Elections/getEVDetails.do"

# The portal encodes the county ID inside onclick attributes such as:
#   onclick="downloadReport('123')"
#   onclick="getReport(123, '...')"
# This regex captures the first numeric token from any onclick value.
_ONCLICK_ID_RE = re.compile(r"['\"]?(\d+)['\"]?")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fetch_ev_details_html(
    session: LegacySession,
    source_election_id: str,
    ev_date: date,
) -> str:
    """POST ``getEVDetails.do`` and return the raw HTML response body.

    Calls :meth:`~texas_turnout_scraper.session.LegacySession.prime_election`
    first (Struts step 2) so the session is ready for EV detail requests.

    Args:
        session: An established :class:`~texas_turnout_scraper.session.LegacySession`.
        source_election_id: SOS election ID string (e.g. ``"49664"``).
        ev_date: Early-voting date to request.

    Returns:
        Raw HTML string from the portal.
    """
    session.prime_election(source_election_id)
    resp = session._post_form(
        _EV_DETAILS_PATH,
        legacy_ev_form_fields(source_election_id, ev_date),
    )
    return resp.text


def fetch_turnout(
    session: LegacySession,
    source_election_id: str,
    ev_date: date,
) -> list[CountyTurnout]:
    """Fetch and parse the county turnout table for one election date.

    Primes the session via ``getElectionEVDates.do``, POSTs to ``getEVDetails.do``
    using the election ID and the EV date in ``"YYYY-MM-DD HH:MM:SS.0"`` format
    (required by the Struts action), then parses the returned HTML for a table
    containing county-level turnout data.

    The portal HTML structure varies across election cycles.  The parser uses
    heuristics (column-count matching, header keyword detection) to locate the
    correct table and map columns, so it is intentionally defensive.

    Args:
        session: An established :class:`~texas_turnout_scraper.session.LegacySession`.
        source_election_id: The SOS numeric ID string (e.g. ``"49664"``).
            Never coerced to int.
        ev_date: The early-voting date to fetch turnout for.

    Returns:
        A list of :class:`~texas_turnout_scraper.models.CountyTurnout` objects,
        one per data row parsed from the HTML table.  Returns an empty list
        when the table cannot be found or all rows are unparseable.

    Example::

        with LegacySession() as sess:
            rows = fetch_turnout(sess, "49664", date(2024, 10, 21))
            for row in rows:
                print(row.county, row.total_in_person_votes)
    """
    html = fetch_ev_details_html(session, source_election_id, ev_date)
    return _parse_turnout_html(html, source_election_id=source_election_id, ev_date=ev_date)


def extract_county_ids(html: str) -> dict[str, str]:
    """Extract the county-name → county-ID mapping from the turnout page HTML.

    The SOS portal embeds county IDs inside ``onclick`` attributes on table
    rows or buttons adjacent to each county's row, e.g.::

        onclick="downloadReport('123')"
        onclick="getReport(123, 'HARRIS')"

    This function scans all elements with an ``onclick`` attribute, extracts
    the first numeric token, and associates it with the nearest county name
    found in the same row or cell.

    Args:
        html: Raw HTML string from the ``getEVDetails.do`` response.

    Returns:
        A ``dict`` mapping normalised county names (upper-case, stripped) to
        their county ID strings.  Returns an empty dict if no IDs can be found.

    Example::

        county_ids = extract_county_ids(resp.text)
        # {"HARRIS": "201", "DALLAS": "113", ...}
    """
    soup = BeautifulSoup(html, "html.parser")
    mapping: dict[str, str] = {}

    # Strategy 1: scan all <tr> elements that have an onclick on the row or a child
    for tr in soup.find_all("tr"):
        onclick_val: str = tr.get("onclick", "")
        if not onclick_val:
            onclick_elem = tr.find(onclick=True)
            if onclick_elem is None:
                continue
            onclick_val = onclick_elem.get("onclick", "")  # type: ignore[assignment]
        id_match = _ONCLICK_ID_RE.search(onclick_val)
        if id_match is None:
            continue

        county_id_str = id_match.group(1)
        county_name = _extract_county_name_from_row(tr)
        if county_name:
            mapping[county_name.upper().strip()] = county_id_str

    # Strategy 2: fall back to any element with an onclick attribute not yet captured
    if not mapping:
        for elem in soup.find_all(onclick=True):
            onclick_val = elem.get("onclick", "")
            id_match = _ONCLICK_ID_RE.search(onclick_val)
            if id_match is None:
                continue
            county_id_str = id_match.group(1)
            # Try to find county name from a sibling or parent text
            parent = elem.find_parent("tr")
            if parent:
                county_name = _extract_county_name_from_row(parent)
                if county_name and county_name.upper().strip() not in mapping:
                    mapping[county_name.upper().strip()] = county_id_str

    logger.debug("extract_county_ids: found %d county→id mappings.", len(mapping))
    return mapping


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_turnout_html(
    html: str,
    source_election_id: str,
    ev_date: date,
) -> list[CountyTurnout]:
    """Parse county turnout rows from the HTML response body.

    Locates the best-matching ``<table>`` in the page and maps its columns to
    :class:`~texas_turnout_scraper.models.CountyTurnout` fields.  The portal
    does not guarantee consistent column ordering across elections, so the
    parser detects column positions from the header row.

    Args:
        html: Raw HTML string.
        source_election_id: SOS election ID string.
        ev_date: The EV date for which turnout was requested.

    Returns:
        List of parsed :class:`~texas_turnout_scraper.models.CountyTurnout`.
    """
    soup = BeautifulSoup(html, "html.parser")
    table = _find_turnout_table(soup)

    if table is None:
        logger.warning(
            "Could not locate turnout table for election %s on %s.",
            source_election_id,
            ev_date,
        )
        return []

    rows = table.find_all("tr")
    if not rows:
        return []

    # Detect header row and column indices
    col_map = _detect_column_map(rows)
    if col_map is None:
        logger.warning(
            "Could not determine column layout for election %s on %s.",
            source_election_id,
            ev_date,
        )
        return []

    results: list[CountyTurnout] = []
    for row in rows[1:]:  # Skip header
        turnout = _parse_row(
            row,
            col_map=col_map,
            source_election_id=source_election_id,
            ev_date=ev_date,
        )
        if turnout is not None:
            results.append(turnout)

    logger.info(
        "Parsed %d county turnout rows for election %s on %s.",
        len(results),
        source_election_id,
        ev_date,
    )
    return results


def _find_turnout_table(soup: BeautifulSoup) -> Tag | None:
    """Locate the county turnout ``<table>`` inside the page.

    Uses three heuristics applied in priority order:

    1. A ``<table>`` whose first row header contains the word "COUNTY".
    2. The ``<table>`` with the most ``<tr>`` rows (likely the data table).
    3. The first ``<table>`` on the page as a last resort.

    Returns:
        The best-matching ``<table>`` tag, or ``None`` if no tables exist.
    """
    tables = soup.find_all("table")
    if not tables:
        return None

    # Heuristic 1: table with a "county" header
    for table in tables:
        header_row = table.find("tr")
        if header_row is None:
            continue
        header_text = header_row.get_text(" ", strip=True).upper()
        if "COUNTY" in header_text:
            return table  # type: ignore[return-value]

    # Heuristic 2: largest table by row count
    return max(tables, key=lambda t: len(t.find_all("tr")))  # type: ignore[return-value]


def _detect_column_map(rows: list[Tag]) -> dict[str, int] | None:
    """Detect column positions from the header row of the turnout table.

    Maps the following logical field names to column indices:

    * ``"county"``
    * ``"registered_voters"``
    * ``"in_person_on_date"``
    * ``"total_in_person"``
    * ``"total_mail"``

    Args:
        rows: All ``<tr>`` elements in the table, header first.

    Returns:
        A dict of field → column index, or ``None`` if the header cannot be
        parsed.
    """
    if not rows:
        return None

    header_row = rows[0]
    cells = header_row.find_all(["th", "td"])
    headers = [c.get_text(" ", strip=True).upper() for c in cells]

    if not headers:
        return None

    col_map: dict[str, int] = {}

    for i, h in enumerate(headers):
        if "COUNTY" in h and "county" not in col_map:
            col_map["county"] = i
        elif "REGISTERED" in h and "registered_voters" not in col_map:
            col_map["registered_voters"] = i
        elif "IN PERSON" in h or "IN-PERSON" in h or "INPERSON" in h:
            # Prefer "on date" column first; cumulative in-person second
            if "DATE" in h or "DAY" in h or " ON DATE" in h:
                col_map["in_person_on_date"] = i
            elif "CUMULATIVE" in h and "total_in_person" not in col_map:
                col_map["total_in_person"] = i
            elif "in_person_on_date" not in col_map and "CUMULATIVE" not in h:
                # First in-person column encountered — treat as on-date
                col_map["in_person_on_date"] = i
        elif ("MAIL" in h or "ABSENTEE" in h) and "total_mail" not in col_map:
            col_map["total_mail"] = i

    # Require at minimum a county name column
    if "county" not in col_map:
        return None

    return col_map


def _parse_row(
    row: Tag,
    col_map: dict[str, int],
    source_election_id: str,
    ev_date: date,
) -> CountyTurnout | None:
    """Parse one ``<tr>`` into a :class:`~texas_turnout_scraper.models.CountyTurnout`.

    Args:
        row: A ``<tr>`` tag from the turnout table body.
        col_map: Column index mapping from :func:`_detect_column_map`.
        source_election_id: SOS election ID string.
        ev_date: The EV date.

    Returns:
        A :class:`CountyTurnout`, or ``None`` for rows that cannot be parsed
        (e.g. subtotal / grand-total rows, blank rows).
    """
    cells = row.find_all(["td", "th"])
    if not cells:
        return None

    def _cell_text(idx: int) -> str:
        if idx >= len(cells):
            return ""
        return cells[idx].get_text(" ", strip=True)

    def _cell_int(idx: int) -> int:
        raw = _cell_text(idx).replace(",", "").strip()
        try:
            return int(raw)
        except (ValueError, TypeError):
            return 0

    county_name = _cell_text(col_map.get("county", 0)).strip()
    if not county_name or county_name.upper() in {
        "COUNTY",
        "TOTAL",
        "GRAND TOTAL",
        "STATEWIDE",
        "",
    }:
        return None

    # Check that this looks like a real data row (county name must be non-numeric)
    if county_name.replace(",", "").replace(".", "").isdigit():
        return None

    registered_voters = _cell_int(col_map.get("registered_voters", -1))
    in_person_on_date = _cell_int(col_map.get("in_person_on_date", -1))
    total_in_person = _cell_int(col_map.get("total_in_person", -1))
    total_mail = _cell_int(col_map.get("total_mail", -1))

    return CountyTurnout(
        election_id=source_election_id,
        report_date=ev_date,
        county=county_name.upper(),
        county_id=None,  # Legacy portal does not expose a numeric county_id
        registered_voters=registered_voters,
        in_person_votes_on_date=in_person_on_date,
        total_in_person_votes=total_in_person,
        total_mail_votes=total_mail,
        roster_available=False,
        source="legacy",
    )


def _extract_county_name_from_row(row: Tag) -> str | None:
    """Extract the county name from a ``<tr>`` tag.

    Looks through each ``<td>`` in the row and returns the text of the first
    cell that looks like an all-alpha county name (no digits, long enough).

    Args:
        row: A ``<tr>`` Tag.

    Returns:
        The county name string, or ``None`` if none is found.
    """
    for cell in row.find_all("td"):
        text = cell.get_text(" ", strip=True).strip()
        # A county name is alphabetic (with spaces/hyphens), at least 3 chars,
        # and does not look like a number or header label.
        if (
            len(text) >= 3
            and not text.replace(",", "").replace(".", "").isdigit()
            and re.match(r"^[A-Za-z][A-Za-z\s\-\.\']+$", text)
        ):
            return text
    return None
