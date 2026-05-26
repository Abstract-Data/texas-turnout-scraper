"""Unit tests for enums.py."""

from texas_turnout_scraper.enums import ElectionType, VoteMethod, infer_election_type


def test_infer_primary_runoff():
    assert (
        infer_election_type("2026 REPUBLICAN PRIMARY RUNOFF ELECTION")
        == ElectionType.PRIMARY_RUNOFF
    )


def test_infer_primary():
    assert infer_election_type("2026 DEMOCRATIC PRIMARY ELECTION") == ElectionType.PRIMARY


def test_infer_general():
    assert infer_election_type("2024 NOVEMBER 5TH GENERAL ELECTION") == ElectionType.GENERAL


def test_infer_special():
    assert infer_election_type("2026 SPECIAL ELECTION SENATE DISTRICT 4") == ElectionType.SPECIAL


def test_infer_constitutional():
    assert (
        infer_election_type("2025 NOVEMBER 4TH CONSTITUTIONAL AMENDMENT")
        == ElectionType.CONSTITUTIONAL_AMENDMENT
    )


def test_infer_unknown():
    assert infer_election_type("SOME UNKNOWN ELECTION TYPE") == ElectionType.UNKNOWN


def test_vote_method_values():
    assert VoteMethod.IN_PERSON == "IN-PERSON"
    assert VoteMethod.MAIL_IN == "MAIL-IN"


def test_election_type_is_str():
    assert isinstance(ElectionType.PRIMARY, str)
