"""Election discovery for the legacy Texas SOS early-voting portal.

Provides two public functions:

* :func:`list_elections` — Fetches the main portal page and parses the
  ``<select name="idElection">`` dropdown to return all known elections as
  :class:`~texas_turnout_scraper.models.LegacyElection` objects.

* :func:`get_ev_dates` — POSTs to ``getElectionEVDates.do`` and parses the
  available early-voting dates for a given election, returning a list of
  :class:`~texas_turnout_scraper.models.LegacyEVDate` objects.

No Selenium. No election_utils. HTTP via :class:`~texas_turnout_scraper.http_transport.PacedHttpClient`
(``cloudscraper`` by default, ``httpx`` in unit tests) + BeautifulSoup.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime

from bs4 import BeautifulSoup

from .models import LegacyElection, LegacyEVDate
from .session import LegacySession

logger = logging.getLogger(__name__)

_ELECTION_DETAILS_PATH = "/Elections/getElectionDetails.do"
_EV_DATES_PATH = "/Elections/getElectionEVDates.do"

# Regex for extracting the year from an election name, e.g. "2024 NOVEMBER…"
_YEAR_RE = re.compile(r"\b(20\d{2}|19\d{2})\b")


def list_elections(session: LegacySession) -> list[LegacyElection]:
    """Fetch the portal's main page and parse the election dropdown.

    The SOS portal serves a page containing a ``<select name="idElection">``
    element populated with all elections known to the system. Each non-empty
    ``<option>`` maps directly to a :class:`~texas_turnout_scraper.models.LegacyElection`.

    Args:
        session: An established :class:`~texas_turnout_scraper.session.LegacySession`.
            The session must already have a valid ``JSESSIONID`` cookie (i.e.
            :meth:`~texas_turnout_scraper.session.LegacySession.establish` must
            have been called, which happens automatically inside a ``with`` block).

    Returns:
        A list of :class:`~texas_turnout_scraper.models.LegacyElection` objects,
        one per non-empty ``<option>`` in the dropdown. The list preserves the
        order returned by the portal. Empty on parse failure.

    Example::

        with LegacySession() as sess:
            elections = list_elections(sess)
            for e in elections:
                print(e.source_election_id, e.election_name)
    """
    html = session.cached_election_html
    if html is None:
        resp = session.get(_ELECTION_DETAILS_PATH)
        html = resp.text

    soup = BeautifulSoup(html, "html.parser")

    select = soup.find("select", {"name": "idElection"})
    if select is None:
        logger.warning("Could not find <select name='idElection'> in portal response.")
        return []

    results: list[LegacyElection] = []
    for option in select.find_all("option"):  # type: ignore[union-attr]
        value = option.get("value", "").strip()
        if not value:
            # Skip the placeholder "-- Select Election --" option
            continue

        name = option.get_text(strip=True)
        year_match = _YEAR_RE.search(name)
        election_year = int(year_match.group(0)) if year_match else None

        results.append(
            LegacyElection(
                source_election_id=str(value),
                election_name=name,
                election_year=election_year,
            )
        )

    logger.debug("Found %d elections from portal dropdown.", len(results))
    return results


def get_ev_dates(session: LegacySession, source_election_id: str) -> list[LegacyEVDate]:
    """Fetch the available early-voting dates for a specific election.

    POSTs to ``getElectionEVDates.do`` with the election ID and parses the
    returned HTML for a date ``<select>`` element. Each ``<option>`` value is
    a datetime string in the form ``"YYYY-MM-DD HH:MM:SS.0"``; the date portion
    is extracted and converted to :class:`datetime.date`.

    Args:
        session: An established :class:`~texas_turnout_scraper.session.LegacySession`.
        source_election_id: The SOS numeric ID string (e.g. ``"49664"``).
            Must remain a string — never coerced to int.

    Returns:
        A list of :class:`~texas_turnout_scraper.models.LegacyEVDate` objects
        ordered as returned by the portal. Empty when no dates are available
        or parsing fails.

    Example::

        with LegacySession() as sess:
            dates = get_ev_dates(sess, "49664")
            for d in dates:
                print(d.date, d.label)
    """
    resp = session.post_form(
        _EV_DATES_PATH,
        {"idElection": source_election_id},
    )
    soup = BeautifulSoup(resp.text, "html.parser")

    # The portal may use several select names; try the most common patterns.
    select = soup.find("select", {"name": re.compile(r"sEVDate|sEVtoronto|evDate", re.I)})
    if select is None:
        # Fall back: grab the first <select> that contains date-like option values
        for sel in soup.find_all("select"):
            opts = sel.find_all("option")
            if any(_looks_like_date_value(o.get("value", "")) for o in opts):
                select = sel
                break

    if select is None:
        logger.warning("Could not find EV date <select> for election %s.", source_election_id)
        return []

    results: list[LegacyEVDate] = []
    for option in select.find_all("option"):  # type: ignore[union-attr]
        raw_value = option.get("value", "").strip()
        if not raw_value or not _looks_like_date_value(raw_value):
            continue

        label = option.get_text(strip=True)
        parsed_date = _parse_ev_date_value(raw_value)
        if parsed_date is None:
            logger.debug("Skipping unparseable EV date value: %r", raw_value)
            continue

        results.append(LegacyEVDate(date=parsed_date, label=label))

    logger.debug("Found %d EV dates for election %s.", len(results), source_election_id)
    return results


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _looks_like_date_value(value: str) -> bool:
    """Return True if *value* looks like a SOS portal EV date option value.

    Acceptable formats:
    * ``"2024-10-21 00:00:00.0"``  (full Struts datetime stamp)
    * ``"2024-10-21"``             (bare ISO date, less common)
    """
    return bool(value) and (value[:4].isdigit() and "-" in value)


def _parse_ev_date_value(raw_value: str) -> datetime.date | None:  # type: ignore[name-defined]
    """Parse the date portion from a SOS portal EV date option value.

    Accepts ``"YYYY-MM-DD HH:MM:SS.0"`` or ``"YYYY-MM-DD"``.

    Returns:
        A :class:`datetime.date`, or ``None`` if parsing fails.
    """
    # Take only the date portion (before any space)
    date_str = raw_value.split(" ")[0].strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(date_str, fmt).date()
        except ValueError:
            continue
    return None
