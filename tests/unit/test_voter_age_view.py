"""The age view on SQLite: whole years as of today and as of the current election's day."""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest

from texas_turnout_scraper.voter_models import Voter, parse_voter, voter_row
from texas_turnout_scraper.voter_tables import (
    AGE_TIERS,
    AGE_VIEW_COLUMNS,
    VOTER_SCHEMA_CREATES,
    VOTER_UPSERT_SQL,
    VOTERFILE_COLUMNS,
    seed_statements,
)

BLANK: str | None = None

ELECTION_DAY = date(2026, 11, 3)


def _years_before(day: date, years: int) -> date:
    try:
        return day.replace(year=day.year - years)
    except ValueError:  # Feb 29 in a common year
        return day.replace(year=day.year - years, day=28)


class _Db:
    def __init__(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        for sql in VOTER_SCHEMA_CREATES:
            self.conn.execute(sql)
        for sql, params in seed_statements():
            self.conn.execute(sql, params)
        self.next_vuid = 1

    def add(self, dob: date | None) -> str:
        vuid = str(self.next_vuid).zfill(10)
        self.next_vuid += 1
        raw: dict[str, str | None] = dict.fromkeys(VOTERFILE_COLUMNS, BLANK)
        raw.update(
            {"VUID": vuid, "SEX": "U", "DOB": None if dob is None else dob.strftime("%Y%m%d")}
        )
        # A far-future "today" so every test birth date is plausible.
        voter = parse_voter(raw, {}, today=date(2100, 1, 1))
        assert isinstance(voter, Voter)
        self.conn.execute(VOTER_UPSERT_SQL, [json.dumps([voter_row(voter)])])
        return vuid

    def ages(self) -> dict[str, tuple[object, ...]]:
        cur = self.conn.execute("SELECT * FROM early_vote_voter_age")
        assert tuple(c[0] for c in cur.description) == AGE_VIEW_COLUMNS
        return {row[0]: row[1:] for row in cur.fetchall()}


@pytest.fixture
def db() -> _Db:
    return _Db()


def test_age_today_and_its_tier_follow_the_calendar_date(db: _Db) -> None:
    today = datetime.now(timezone.utc).date()  # date('now') in SQLite is the UTC date
    cases = {
        db.add(_years_before(today, 18)): (18, "18-24"),
        db.add(_years_before(today + timedelta(days=1), 25)): (24, "18-24"),
        db.add(_years_before(today, 35)): (35, "35-54"),
        db.add(_years_before(today, 90)): (90, "85+"),
        db.add(_years_before(today + timedelta(days=1), 18)): (17, None),
        db.add(None): (None, None),
    }
    ages = db.ages()
    for vuid, (age, tier) in cases.items():
        assert ages[vuid][:2] == (age, tier), vuid


def test_age_on_election_day_uses_the_current_calendar_row(db: _Db) -> None:
    eighteen = db.add(date(2008, 11, 3))
    seventeen = db.add(date(2008, 11, 4))
    none = db.add(None)
    ages = db.ages()
    assert ages[eighteen][2:] == (18, "18-24")
    assert ages[seventeen][2:] == (17, None)
    assert ages[none][2:] == (None, None)


@pytest.mark.parametrize(("reference", "age"), [("2027-02-28", 18), ("2027-03-01", 19)])
def test_a_feb_29_birthday_has_passed_on_march_1(db: _Db, reference: str, age: int) -> None:
    leapling = db.add(date(2008, 2, 29))
    db.conn.execute(
        "UPDATE early_vote_election_calendar SET election_date = ? WHERE is_current = 1",
        [reference],
    )
    assert db.ages()[leapling][2] == age


def test_every_tier_boundary_comes_from_the_lookup_table(db: _Db) -> None:
    expected: dict[str, tuple[int, str]] = {}
    for low, high, label, _ in AGE_TIERS:
        for age in {low, min(high, 120)}:
            expected[db.add(_years_before(ELECTION_DAY, age))] = (age, label)
    ages = db.ages()
    for vuid, value in expected.items():
        assert ages[vuid][2:] == value, value
    db.conn.execute("UPDATE age_tier SET label = 'renamed' WHERE label = '85+'")
    assert "renamed" in {row[3] for row in db.ages().values()}
