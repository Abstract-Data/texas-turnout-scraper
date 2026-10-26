#!/usr/bin/env python3
"""Pull one Civix day file locally and print the numbers ``GET /spike/stage`` reports.

Temporary: this exists to settle ADR-014 risk R2 (does the Worker's canonical sha256 match the
loader's?) and the ``occ`` half of R4. It makes exactly one portal request, decodes the envelope
with the loader's own ``_decode_envelope``, and runs the loader's ``canonicalize_roster_csv`` and
``keyed_lines`` over the whole file, the way the 32 GB runner does today.

Run it once per day file and compare each line with the Worker's JSON:

    uv run python scripts/spike_compare_canonical.py 53815 2026-10-20

    raw_sha256          <- /spike/stage  "raw_sha256"
    canonical_sha256    <- /spike/stage  "sha256"       (and early_vote_day.sha256)
    rows                <- /spike/stage  "rows"
    rows_per_range[K]   <- /spike/stage  "rows_per_range"[K]
    keys_sha256[K]      <- POST /spike/range-write?...&range=K&verify=1  "keys_sha256"

It prints counts and hashes only: no voter name, no VUID, no cell. Delete it with the rest of
the spike.
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import sys
from datetime import date
from typing import Any

import httpx

from texas_turnout_scraper.civix.envelope import _decode_envelope
from texas_turnout_scraper.early_vote_roster import canonicalize_roster_csv
from texas_turnout_scraper.early_vote_roster_rows import KEY_RANGES, key_ranges, keyed_lines

CIVIX_GET_FILE_URL = "https://goelect.txelections.civixapps.com/api-ivis-system/api/v1/getFile"
USER_AGENT = "tx-turnout-ev-api-preview spike (Abstract Data)"
TIMEOUT_SECONDS = 180.0

_RANGE_LOWS = tuple(int(low) for low, _ in key_ranges())


def range_of(value: int) -> int:
    """The same expression ``ev_api.spike_stage.range_of`` uses in the Worker."""
    return bisect.bisect_right(_RANGE_LOWS, value) - 1


def portal_url(election_id: str, ev_date: date) -> str:
    query = f"type=EVR_STATEWIDE&electionId={election_id}&electionDate={ev_date:%m/%d/%Y}"
    return f"{CIVIX_GET_FILE_URL}?{query}"


def fetch_day(election_id: str, ev_date: date) -> bytes:
    """One request, one decode. Nothing is retried, so a rate-limited portal is not hammered."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    with httpx.Client(timeout=TIMEOUT_SECONDS, follow_redirects=True) as client:
        response = client.get(portal_url(election_id, ev_date), headers=headers)
    content_type = response.headers.get("content-type", "")
    if response.status_code != 200 or "html" in content_type.lower():
        raise SystemExit(
            f"portal returned status={response.status_code} content_type={content_type}"
        )
    return _decode_envelope(response)


def report(decoded: bytes) -> dict[str, Any]:
    canonical, summary = canonicalize_roster_csv(decoded)
    rows_per_range = [0] * KEY_RANGES
    hashers = [hashlib.sha256() for _ in range(KEY_RANGES)]
    whole = hashlib.sha256()
    rows = 0
    for (digest, occ), _ in keyed_lines(decoded):
        rows += 1
        index = range_of(digest)
        rows_per_range[index] += 1
        pair = f"{digest}:{occ}\n".encode()
        hashers[index].update(pair)
        whole.update(pair)
    return {
        "decoded_bytes": len(decoded),
        "raw_sha256": hashlib.sha256(decoded).hexdigest(),
        "canonical_bytes": len(canonical),
        "canonical_sha256": hashlib.sha256(canonical).hexdigest(),
        "rows": rows,
        "summary_est_rows": summary.est_rows,
        "header_columns": len(summary.columns),
        "voting_method_counts": list(summary.voting_method_counts),
        "rows_per_range": rows_per_range,
        "keys_sha256_all": whole.hexdigest(),
        "keys_sha256_per_range": [hasher.hexdigest() for hasher in hashers],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("election_id", help="source election id, e.g. 53815")
    parser.add_argument("ev_date", help="early-voting date as YYYY-MM-DD")
    parser.add_argument(
        "--from-file",
        help="read already-decoded bytes from this path instead of calling the portal",
    )
    args = parser.parse_args(argv)

    if args.from_file:
        with open(args.from_file, "rb") as handle:
            decoded = handle.read()
    else:
        decoded = fetch_day(args.election_id, date.fromisoformat(args.ev_date))

    numbers = report(decoded)
    print(f"election_id         {args.election_id}")
    print(f"ev_date             {args.ev_date}")
    for name in (
        "decoded_bytes",
        "raw_sha256",
        "canonical_bytes",
        "canonical_sha256",
        "rows",
        "summary_est_rows",
        "header_columns",
        "keys_sha256_all",
    ):
        print(f"{name:<20}{numbers[name]}")
    print(f"{'voting_method':<20}{numbers['voting_method_counts']}")
    for index in range(KEY_RANGES):
        rows = numbers["rows_per_range"][index]
        digest = numbers["keys_sha256_per_range"][index]
        print(f"range {index:02d}            rows={rows:<10} keys_sha256={digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
