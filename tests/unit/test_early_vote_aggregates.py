"""Aggregate-only early-vote JSON builders."""

from __future__ import annotations

import json

from texas_turnout_scraper.early_vote_aggregates import (
    aggregates_from_decoded,
    aggregates_from_roster_csv,
    aggregates_from_turnout_json,
    build_by_day_series,
    slice_by_method,
)


def test_turnout_json_statewide_and_county_totals() -> None:
    payload = {
        "turnout_by_county": [
            {
                "county": "TRAVIS",
                "total_in_person_votes_for_election": 10,
                "total_mail_votes_for_election": 2,
            },
            {
                "county": "HARRIS",
                "total_in_person_votes_for_election": 5,
                "total_mail_votes_for_election": 1,
            },
        ]
    }
    body = aggregates_from_turnout_json(
        json.dumps(payload).encode(),
        source_election_id="53815",
        ev_date="2026-10-20",
    )
    assert body["source_election_id"] == "53815"
    assert body["ev_date"] == "2026-10-20"
    assert body["statewide"] == {"total": 18, "in_person": 15, "mail_in": 3}
    assert len(body["by_county"]) == 2


def test_roster_csv_method_split_without_pii_fields() -> None:
    csv = (
        b"COUNTY,VOTER_NAME,ID_VOTER,VOTING_METHOD,PRECINCT\n"
        b"TRAVIS,Secret,0000000001,IN-PERSON,1\n"
        b"TRAVIS,Secret,0000000002,MAIL-IN,1\n"
        b"HARRIS,Secret,0000000003,IN-PERSON,2\n"
    )
    body = aggregates_from_roster_csv(csv, source_election_id="53815")
    assert body["statewide"]["total"] == 3
    assert body["statewide"]["in_person"] == 2
    assert body["statewide"]["mail_in"] == 1
    assert "VOTER_NAME" not in json.dumps(body)
    assert "0000000001" not in json.dumps(body)


def test_by_day_series_and_by_method_slice() -> None:
    day_one = aggregates_from_turnout_json(
        json.dumps(
            {
                "turnout_by_county": [
                    {
                        "county": "TRAVIS",
                        "total_in_person_votes_for_election": 1,
                        "total_mail_votes_for_election": 0,
                    }
                ]
            }
        ).encode(),
        source_election_id="53815",
        ev_date="2026-10-19",
    )
    series = build_by_day_series(
        source_election_id="53815",
        days=(("2026-10-19", day_one),),
    )
    assert series["series"][0]["ev_date"] == "2026-10-19"
    assert series["series"][0]["total"] == 1
    methods = slice_by_method(day_one)
    assert methods["statewide"]["in_person"] == 1


def test_detect_payload_kind() -> None:
    turnout = aggregates_from_decoded(
        b'{"turnout_by_county": []}',
        source_election_id="49664",
    )
    assert turnout["payload_kind"] == "turnout"
    roster = aggregates_from_decoded(
        b"COUNTY,VOTING_METHOD\nTRAVIS,IN-PERSON\n",
        source_election_id="49664",
    )
    assert roster["payload_kind"] == "statewide_roster"
