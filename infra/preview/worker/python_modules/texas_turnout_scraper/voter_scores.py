"""Scores derived from a voter's history: pure functions, no sqlmodel, versioned by SCORE_VERSION.

A history code's suffix gives the mode (``E`` early in person, ``A`` absentee by mail, none
Election Day); the first letter of a primary or runoff code is the party. Counts cover the 17
voterfile columns. The dated early days (``GEN25``, ``PRI26``, ``RO26``) give the timing baseline
only. See docs/adr/011-voter-attributes.md.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

from texas_turnout_scraper.voter_tables import (
    CALENDAR_BY_KEY,
    HISTORY_COLUMNS,
    MIDTERM_GENERALS,
    OFF_YEAR_GENERALS,
    PRESIDENTIAL_GENERALS,
    PRIMARIES_AND_RUNOFFS,
)

Mode = Literal["early", "absentee", "election_day"]
History = Mapping[str, str | None]
Dated = Mapping[str, tuple[int, str] | None]

_SUFFIX_MODES: Mapping[str, Mode] = {"E": "early", "A": "absentee"}


@dataclass(frozen=True)
class Derived:
    n_voted: int
    n_early: int
    n_absentee: int
    n_election_day: int
    n_gen_pres: int
    n_gen_mid: int
    n_gen_off: int
    n_primary_r: int
    n_primary_d: int
    primary_pattern: str
    general_pattern: str
    typical_mode: str | None
    early_or_absentee_share: float | None
    typical_earliness: float | None
    earliness_n: int


def mode_of(code: str) -> Mode:
    return _SUFFIX_MODES.get(code[-1], "election_day") if len(code) > 1 else "election_day"


def earliness(day: int, days: int) -> float:
    """1.0 on the first early day, 0.0 on the last."""
    return (days - day) / (days - 1)


def _pattern(first: int, second: int, labels: tuple[str, str, str, str]) -> str:
    both, first_only, second_only, neither = labels
    if first and second:
        return both
    if first:
        return first_only
    return second_only if second else neither


def _typical(modes: Mapping[Mode, int]) -> str | None:
    ranked = sorted(modes.values(), reverse=True)
    if ranked[0] == 0:
        return None
    if ranked[0] == ranked[1]:
        return "mixed"
    return max(modes.items(), key=lambda item: item[1])[0]


def derive(history: History, dated: Dated) -> Derived:
    voted = {c: code for c in HISTORY_COLUMNS if (code := history.get(c)) is not None and code}
    modes: dict[Mode, int] = {"early": 0, "absentee": 0, "election_day": 0}
    for code in voted.values():
        modes[mode_of(code)] += 1
    parties = [voted[c][0] for c in PRIMARIES_AND_RUNOFFS if c in voted]
    n_r, n_d = parties.count("R"), parties.count("D")
    n_pres = sum(c in voted for c in PRESIDENTIAL_GENERALS)
    n_mid = sum(c in voted for c in MIDTERM_GENERALS)
    n_voted = len(voted)
    timings = [
        earliness(entry[0], CALENDAR_BY_KEY[key].early_days)
        for key, entry in sorted(dated.items())
        if entry is not None
    ]
    return Derived(
        n_voted=n_voted,
        n_early=modes["early"],
        n_absentee=modes["absentee"],
        n_election_day=modes["election_day"],
        n_gen_pres=n_pres,
        n_gen_mid=n_mid,
        n_gen_off=sum(c in voted for c in OFF_YEAR_GENERALS),
        n_primary_r=n_r,
        n_primary_d=n_d,
        primary_pattern=_pattern(n_r, n_d, ("mixed", "straight_r", "straight_d", "none")),
        general_pattern=_pattern(
            n_pres, n_mid, ("both", "presidential_only", "midterm_only", "none")
        ),
        typical_mode=_typical(modes),
        early_or_absentee_share=(
            round((modes["early"] + modes["absentee"]) / n_voted, 4) if n_voted else None
        ),
        typical_earliness=round(sum(timings) / len(timings), 4) if timings else None,
        earliness_n=len(timings),
    )


def src_hash(values: Iterable[str | int | None]) -> int:
    """Signed 64-bit BLAKE2b over length-prefixed UTF-8; None hashes apart from the empty text."""
    hasher = hashlib.blake2b(digest_size=8)
    for value in values:
        if value is None:
            hasher.update(b"\xff\xff\xff\xff")
            continue
        data = str(value).encode("utf-8")
        hasher.update(len(data).to_bytes(4, "big"))
        hasher.update(data)
    return int.from_bytes(hasher.digest(), "big", signed=True)
