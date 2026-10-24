"""Voter history scores: one pure function over the 17 voterfile codes and the dated early days."""

from __future__ import annotations

import dataclasses
import os
import subprocess
import sys
from collections.abc import Mapping
from typing import Any, cast

import pytest

from texas_turnout_scraper.voter_scores import Derived, derive, earliness, mode_of, src_hash
from texas_turnout_scraper.voter_tables import HISTORY_COLUMNS

NO_HISTORY: Mapping[str, str | None] = dict.fromkeys(HISTORY_COLUMNS, cast("str | None", None))


def _history(**codes: str) -> dict[str, str | None]:
    return {**NO_HISTORY, **codes}


@pytest.mark.parametrize(
    ("code", "mode"),
    [
        ("V", "election_day"),
        ("VE", "early"),
        ("VA", "absentee"),
        ("D", "election_day"),
        ("R", "election_day"),
        ("DE", "early"),
        ("RE", "early"),
        ("DA", "absentee"),
        ("RA", "absentee"),
    ],
)
def test_every_code_maps_to_its_mode(code: str, mode: str) -> None:
    assert mode_of(code) == mode


NEVER = Derived(
    n_voted=0,
    n_early=0,
    n_absentee=0,
    n_election_day=0,
    n_gen_pres=0,
    n_gen_mid=0,
    n_gen_off=0,
    n_primary_r=0,
    n_primary_d=0,
    primary_pattern="none",
    general_pattern="none",
    typical_mode=None,
    early_or_absentee_share=None,
    typical_earliness=None,
    earliness_n=0,
)


def _expect(**changes: Any) -> Derived:
    return dataclasses.replace(NEVER, **changes)


CASES: list[tuple[str, dict[str, str | None], Derived]] = [
    ("all blank", NO_HISTORY, NEVER),
    (
        "only absentee, presidential generals",
        _history(GEN24="VA", GEN20="VA"),
        _expect(
            n_voted=2,
            n_absentee=2,
            n_gen_pres=2,
            general_pattern="presidential_only",
            typical_mode="absentee",
            early_or_absentee_share=1.0,
        ),
    ),
    (
        "tie of the top two modes, both generals",
        _history(GEN24="VE", GEN22="V"),
        _expect(
            n_voted=2,
            n_early=1,
            n_election_day=1,
            n_gen_pres=1,
            n_gen_mid=1,
            general_pattern="both",
            typical_mode="mixed",
            early_or_absentee_share=0.5,
        ),
    ),
    (
        "midterm generals only",
        _history(GEN22="V", GEN18="V"),
        _expect(
            n_voted=2,
            n_election_day=2,
            n_gen_mid=2,
            general_pattern="midterm_only",
            typical_mode="election_day",
            early_or_absentee_share=0.0,
        ),
    ),
    (
        "only off-year generals do not decide the label",
        _history(GEN23="V", GEN21="VE", GEN19="VE"),
        _expect(
            n_voted=3,
            n_early=2,
            n_election_day=1,
            n_gen_off=3,
            typical_mode="early",
            early_or_absentee_share=0.6667,
        ),
    ),
    (
        "D primaries and a D runoff",
        _history(PRI24="DE", RO24="D", PRI18="DA"),
        _expect(
            n_voted=3,
            n_early=1,
            n_absentee=1,
            n_election_day=1,
            n_primary_d=3,
            primary_pattern="straight_d",
            typical_mode="mixed",
            early_or_absentee_share=0.6667,
        ),
    ),
    (
        "R primary and R runoff",
        _history(PRI22="R", RO26="RE"),
        _expect(
            n_voted=2,
            n_early=1,
            n_election_day=1,
            n_primary_r=2,
            primary_pattern="straight_r",
            typical_mode="mixed",
            early_or_absentee_share=0.5,
        ),
    ),
    (
        "a D primary and an R runoff are mixed",
        _history(PRI20="D", RO22="RA"),
        _expect(
            n_voted=2,
            n_absentee=1,
            n_election_day=1,
            n_primary_r=1,
            n_primary_d=1,
            primary_pattern="mixed",
            typical_mode="mixed",
            early_or_absentee_share=0.5,
        ),
    ),
    (
        "a clear top mode with a tie below it",
        _history(GEN24="VE", GEN22="VE", GEN20="VE", PRI24="R", RO24="RA"),
        _expect(
            n_voted=5,
            n_early=3,
            n_absentee=1,
            n_election_day=1,
            n_gen_pres=2,
            n_gen_mid=1,
            n_primary_r=2,
            primary_pattern="straight_r",
            general_pattern="both",
            typical_mode="early",
            early_or_absentee_share=0.8,
        ),
    ),
]


@pytest.mark.parametrize(("history", "expected"), [c[1:] for c in CASES], ids=[c[0] for c in CASES])
def test_counts_and_labels(history: Mapping[str, str | None], expected: Derived) -> None:
    assert derive(history, {}) == expected


@pytest.mark.parametrize(
    ("day", "days", "value"),
    [(1, 11, 1.0), (11, 11, 0.0), (6, 11, 0.5), (1, 5, 1.0), (5, 5, 0.0), (4, 12, 8 / 11)],
)
def test_earliness_is_one_on_the_first_early_day_and_zero_on_the_last(
    day: int, days: int, value: float
) -> None:

    assert earliness(day, days) == pytest.approx(value)


def test_typical_earliness_averages_the_dated_elections_and_ignores_gen25_in_counts() -> None:
    derived = derive(
        _history(PRI26="RE", RO26="RA"),
        {"PRI26": (6, "E"), "RO26": (3, "A"), "GEN25": (1, "E")},
    )
    # PRI26 day 6 of 11 is 0.5, RO26 day 3 of 5 is 0.5, GEN25 day 1 of 12 is 1.0.
    assert derived.typical_earliness == pytest.approx(0.6667)
    assert derived.earliness_n == 3
    assert derived.n_voted == 2


def test_no_dated_vote_gives_no_earliness() -> None:
    derived = derive(_history(PRI26="R"), {"PRI26": None, "RO26": None, "GEN25": None})
    assert (derived.typical_earliness, derived.earliness_n) == (None, 0)


def test_golden_row() -> None:
    history = _history(
        GEN24="VE",
        GEN23="V",
        GEN22="VA",
        GEN20="VE",
        GEN18="V",
        PRI26="DE",
        PRI24="D",
        RO24="DE",
        PRI18="RA",
    )
    assert derive(history, {"PRI26": (11, "E"), "GEN25": (12, "E")}) == Derived(
        n_voted=9,
        n_early=4,
        n_absentee=2,
        n_election_day=3,
        n_gen_pres=2,
        n_gen_mid=2,
        n_gen_off=1,
        n_primary_r=1,
        n_primary_d=3,
        primary_pattern="mixed",
        general_pattern="both",
        typical_mode="early",
        early_or_absentee_share=0.6667,
        typical_earliness=0.0,
        earliness_n=2,
    )


def test_src_hash_is_stable_and_sees_every_input() -> None:
    values = ["1000000001", "F", "19800101", None, "VE", ""]
    assert src_hash(values) == src_hash(list(values))
    assert -(2**63) <= src_hash(values) < 2**63
    assert src_hash(values) != src_hash([*values[:3], "", *values[4:]])
    assert src_hash(["ab", "c"]) != src_hash(["a", "bc"])
    assert src_hash([None]) != src_hash([""])


def test_src_hash_does_not_depend_on_pythonhashseed() -> None:
    code = (
        "from texas_turnout_scraper.voter_scores import src_hash;"
        "print(src_hash(['1000000001', 'F', None, 'VE', 3]))"
    )
    outputs = {
        subprocess.run(
            [sys.executable, "-c", code],
            check=True,
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONHASHSEED": seed},
        ).stdout
        for seed in ("0", "1", "12345")
    }
    assert len(outputs) == 1
