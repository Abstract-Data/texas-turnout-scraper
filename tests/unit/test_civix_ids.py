from __future__ import annotations

import pytest

from texas_turnout_scraper.civix.ids import normalize_election_id, require_election_id


@pytest.mark.parametrize(
    ("value", "expected"),
    [("49664", "49664"), (" 49664 ", "49664"), ("0049664", "0049664"), (49664, "49664")],
)
def test_accepts_ascii_digits(value: object, expected: str) -> None:
    assert normalize_election_id(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        chr(0x664) + chr(0x669),
        chr(0xFF14) + chr(0xFF19),
        "4" + chr(0xB2),
        chr(0xB2),
        "0",
        "000",
        "",
        "12a",
        -1,
        0,
        True,
        None,
    ],
)
def test_rejects_non_ascii_digits_and_non_positive(value: object) -> None:
    assert normalize_election_id(value) is None
    with pytest.raises(ValueError, match="election id"):
        require_election_id(value)
