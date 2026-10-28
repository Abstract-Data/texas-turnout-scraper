"""A VOTER_GEO_PAGE_SQL page of the largest day stays under D1's 2 MB string limit."""

from __future__ import annotations

import math
import random
import sqlite3

from texas_turnout_scraper.early_vote_roster_rows import ROW_TABLE_SQL, key_ranges
from texas_turnout_scraper.voter_geo_tables import (
    COORD_DECIMALS,
    VOTER_GEO_PAGE_SQL,
    VOTER_GEO_PAGES,
    VOTER_GEO_SCHEMA_CREATES,
)

# D1's maximum string, BLOB or row size: https://developers.cloudflare.com/d1/platform/limits/
D1_MAX_STRING_BYTES = 2_000_000
# The working ceiling per page, leaving room under the limit.
PAGE_BUDGET_BYTES = 1_500_000
# The largest day to plan for. 2026-10-24 (early day 11) re-listed 656,734 earlier voters on top
# of its own (ADR-012), about 1M rows against an average day near 340,000.
LARGEST_DAY_ROWS = 1_000_000
# Row hashes are uniform, so a page's count is binomial: 10% over the mean is far past 6 sigma.
PAGE_SLACK = 1.1
DAY = 1


def _widest_page(voters: int) -> tuple[int, str]:
    """One page of ``voters`` live VUIDs, each at the widest a voter_geo page row gets.

    A Stage 2 point is the widest voter: it carries coordinates, the long geocode source and the
    vintage, where a Stage 1 voter has nulls. Codes are at their real maximum widths.
    """
    conn = sqlite3.connect(":memory:")
    for sql in (ROW_TABLE_SQL, *VOTER_GEO_SCHEMA_CREATES):
        conn.execute(sql)
    low, high = key_ranges(1)[0]
    rng = random.Random(11)
    for n in range(voters):
        vuid = f"{9_000_000_000 + n:010d}"
        conn.execute(
            "INSERT INTO early_vote_roster_row VALUES "
            "('HARRIS', 'FAKE PERSON', ?, 'IN-PERSON', '0001', ?, ?, 0, '{}', 1, NULL)",
            (vuid, DAY, rng.randrange(int(low), int(high) + 1)),
        )
        conn.execute(
            "INSERT INTO voter_geo VALUES "
            "(?, '201', '2010001', '38', '17', '150', 7, 3, 1, '2026-10-27T01:00:00Z')",
            (vuid,),
        )
        conn.execute(
            "INSERT INTO voter_geo_point VALUES (?, ?, ?, '38', '17', '150', "
            "'txgio_address_point', 'address_point', '2026-03-18', 1, '2026-10-28T01:00:00Z')",
            (
                vuid,
                round(rng.uniform(25.8, 36.5), COORD_DECIMALS),
                round(rng.uniform(-106.65, -100.0), COORD_DECIMALS),
            ),
        )
    count, text = conn.execute(VOTER_GEO_PAGE_SQL, (DAY, low, high)).fetchone()
    conn.close()
    return count, text


def test_largest_day_page_stays_under_d1_string_limit() -> None:
    voters = math.ceil(LARGEST_DAY_ROWS / VOTER_GEO_PAGES * PAGE_SLACK)
    count, text = _widest_page(voters)
    size = len(text.encode())

    assert count == voters
    assert size < D1_MAX_STRING_BYTES
    assert size <= PAGE_BUDGET_BYTES, (
        f"{voters} voters make a {size:,}-byte page; raise VOTER_GEO_PAGES"
    )
