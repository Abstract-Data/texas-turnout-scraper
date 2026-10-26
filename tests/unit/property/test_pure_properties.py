"""Properties of the pure helpers and their boundaries, on synthetic data only.

Covers the Worker cron schedule (Central wall clock across the DST switch and the end of the
window), the dispatch request and its token handling, the voter history scores and ``src_hash``,
roster line keys and ``key_ranges``, the D1 chunking and size estimates, repeat rows, the bounded
JSON payload batches, the election-day age view on SQLite, and the precinct and county matching
helpers of the geography load.
"""

from __future__ import annotations

import asyncio
import base64
import csv
import hashlib
import io
import json
import logging
import os
import sqlite3
import subprocess
import sys
import textwrap
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from ev_api.cron import (
    CRON_SCHEDULE,
    DispatchError,
    dispatch_request,
    run_scheduled,
    should_dispatch,
)
from hypothesis import example, given, settings
from hypothesis import strategies as st

from texas_turnout_scraper import early_vote_sync
from texas_turnout_scraper.early_vote_roster_rows import (
    KEY_RANGES,
    VOTING_METHODS,
    day_number,
    key_ranges,
    keyed_lines,
)
from texas_turnout_scraper.early_vote_sync import (
    CHUNK_RAW_BYTES,
    D1_DATABASE_CAP_BYTES,
    EarlyVoteCapError,
    EarlyVoteSyncError,
    assert_within_cap,
    estimated_stored_bytes,
    iter_chunk_b64,
    sha256_decoded,
)
from texas_turnout_scraper.geo_load import county_key, resolve_precinct
from texas_turnout_scraper.geo_tables import ALIAS_RULES
from texas_turnout_scraper.repeat_build import repeat_record
from texas_turnout_scraper.repeat_tables import (
    FLAG_CROSS_DAY,
    FLAG_CROSS_METHOD,
    FLAG_REMOVED,
    FLAG_SAME_DAY,
)
from texas_turnout_scraper.voter_link import payload_batches
from texas_turnout_scraper.voter_models import Voter, parse_voter, voter_row
from texas_turnout_scraper.voter_scores import derive, earliness, src_hash
from texas_turnout_scraper.voter_tables import (
    AGE_TIERS,
    CALENDAR_BY_KEY,
    DATED_KEYS,
    GENERAL_CODES,
    HISTORY_COLUMNS,
    MIDTERM_GENERALS,
    OFF_YEAR_GENERALS,
    PRESIDENTIAL_GENERALS,
    PRIMARY_CODES,
    VOTER_SCHEMA_CREATES,
    VOTER_UPSERT_SQL,
    VOTERFILE_COLUMNS,
    seed_statements,
)

INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1
CENTRAL = ZoneInfo("America/Chicago")
UTC = timezone.utc

# --------------------------------------------------------------------------- cron

# The oracle is tzdata. The cron module hard-codes one switch, so the comparison holds from the
# 2026 spring change (2026-03-08 08:00 UTC) on.
WINDOW_START = datetime(2026, 3, 9, tzinfo=UTC)
WINDOW_END = datetime(2026, 11, 12, tzinfo=UTC)
LAST_RUN = datetime(2026, 11, 4, 5, 59, 59, tzinfo=UTC)
WINDOW_HOURS = int((WINDOW_END - WINDOW_START).total_seconds() // 3600)
CRON_HOURS = frozenset(int(h) for h in CRON_SCHEDULE.split()[1].split(","))
DISPATCH_INPUTS = {
    "sync_mode": "backfill",
    "election_id": "53815",
    "election_date": "",
    "dry_run": "false",
    "payload_kind": "statewide_roster",
    "trigger": "cron",
}

utc_hours = st.integers(0, WINDOW_HOURS).map(lambda h: WINDOW_START + timedelta(hours=h))


def _central_hour_due(now: datetime) -> bool:
    return now <= LAST_RUN and now.astimezone(CENTRAL).hour in {10, 17, 22}


@given(utc_hours)
@example(datetime(2026, 11, 1, 3, tzinfo=UTC))
@example(datetime(2026, 11, 1, 4, tzinfo=UTC))
@example(datetime(2026, 11, 1, 6, tzinfo=UTC))
@example(datetime(2026, 11, 1, 7, tzinfo=UTC))
@example(datetime(2026, 11, 1, 16, tzinfo=UTC))
@example(datetime(2026, 11, 4, 4, tzinfo=UTC))
@example(datetime(2026, 11, 4, 5, tzinfo=UTC))
@example(datetime(2026, 11, 4, 16, tzinfo=UTC))
@example(datetime(2026, 12, 31, 16, tzinfo=UTC))
@example(datetime(2027, 1, 1, 4, tzinfo=UTC))
def test_should_dispatch_is_central_ten_five_and_ten_until_the_end(now: datetime) -> None:
    assert should_dispatch(now) == _central_hour_due(now)


@given(utc_hours, st.integers(0, 59), st.integers(0, 59))
@example(datetime(2026, 11, 1, 6, tzinfo=UTC), 59, 59)
@example(datetime(2026, 11, 4, 4, tzinfo=UTC), 59, 59)
def test_a_late_trigger_within_the_hour_decides_like_the_top_of_the_hour(
    now: datetime, minute: int, second: int
) -> None:
    assert should_dispatch(now.replace(minute=minute, second=second)) == should_dispatch(now)
    if should_dispatch(now):
        assert now.hour in CRON_HOURS


@given(st.dates(date(2026, 3, 9), date(2026, 11, 10)))
@example(date(2026, 10, 31))
@example(date(2026, 11, 1))
@example(date(2026, 11, 3))
@example(date(2026, 11, 4))
def test_each_central_day_dispatches_three_times_until_election_day(day: date) -> None:
    midnight = datetime(day.year, day.month, day.day, tzinfo=CENTRAL).astimezone(UTC)
    next_midnight = (
        datetime(day.year, day.month, day.day, tzinfo=CENTRAL) + timedelta(days=1)
    ).astimezone(UTC)
    hours = int((next_midnight - midnight).total_seconds() // 3600)
    fired = sum(should_dispatch(midnight + timedelta(hours=h)) for h in range(hours))
    assert fired == (3 if day <= date(2026, 11, 3) else 0)


_ALL_HOURS = [WINDOW_START + timedelta(hours=h) for h in range(WINDOW_HOURS + 1)]
due_hours = st.sampled_from([t for t in _ALL_HOURS if _central_hour_due(t)])
idle_hours = st.sampled_from([t for t in _ALL_HOURS if not _central_hour_due(t)])

tokens = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_", min_size=12
).map(lambda t: f"tok_{t}")


@given(tokens)
def test_dispatch_request_carries_the_token_only_in_the_authorization_header(token: str) -> None:
    url, headers, body = dispatch_request(token)
    assert url.startswith("https://api.github.com/repos/")
    assert url.endswith("/actions/workflows/early-vote-sync.yml/dispatches")
    assert headers["Authorization"] == f"Bearer {token}"
    assert headers["Accept"] == "application/vnd.github+json"
    assert body == {"ref": "main", "inputs": DISPATCH_INPUTS}
    others = [url, json.dumps(body), *(v for k, v in headers.items() if k != "Authorization")]
    assert all(token not in text for text in others)
    body["inputs"]["election_id"] = "0"  # type: ignore[index]  # reason: body is a plain dict
    assert dispatch_request(token)[2] == {"ref": "main", "inputs": DISPATCH_INPUTS}


class _Records(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


class _Env:
    def __init__(self, token: str | None) -> None:
        if token is not None:
            self.GITHUB_DISPATCH_TOKEN = token


def _run_scheduled(env: _Env, now: datetime, status: int) -> tuple[list[object], list[str]]:
    calls: list[object] = []

    async def send(url: str, headers: dict[str, str], body: dict[str, object]) -> int:
        calls.append((url, headers, body))
        return status

    logger = logging.getLogger("ev_api.cron")
    records = _Records()
    level = logger.level
    logger.addHandler(records)
    logger.setLevel(logging.DEBUG)
    try:
        asyncio.run(run_scheduled(env, now, send))
    finally:
        logger.removeHandler(records)
        logger.setLevel(level)
    return calls, records.lines


@given(due_hours, tokens, st.integers(0, 999))
@example(datetime(2026, 11, 2, 16, tzinfo=UTC), "tok_example_value_1", 204)
@example(datetime(2026, 11, 2, 16, tzinfo=UTC), "tok_example_value_1", 200)
@example(datetime(2026, 11, 2, 16, tzinfo=UTC), "tok_example_value_1", 0)
def test_only_204_counts_as_a_dispatch_and_the_token_never_leaks(
    now: datetime, token: str, status: int
) -> None:
    error: DispatchError | None = None
    try:
        calls, lines = _run_scheduled(_Env(token), now, status)
    except DispatchError as raised:
        error = raised
        calls, lines = [], []
    assert (error is None) == (status == 204)
    if error is not None:
        assert token not in str(error)
        assert str(status) in str(error)
    else:
        assert len(calls) == 1
    assert all(token not in line for line in lines)


@given(utc_hours, st.sampled_from([None, ""]))
def test_a_missing_token_sends_nothing(now: datetime, token: str | None) -> None:
    calls, lines = _run_scheduled(_Env(token), now, 204)
    assert calls == []
    assert bool(lines) == should_dispatch(now)
    assert all("GITHUB_DISPATCH_TOKEN" in line for line in lines)


@given(idle_hours, tokens)
def test_off_schedule_hours_send_nothing(now: datetime, token: str) -> None:
    calls, _ = _run_scheduled(_Env(token), now, 500)
    assert calls == []


# --------------------------------------------------------------------------- voter scores

_MISSING = object()
GENERAL_COLUMNS = frozenset(c for c in HISTORY_COLUMNS if c.startswith("GEN"))


@st.composite
def histories(draw: st.DrawFn) -> dict[str, str | None]:
    history: dict[str, str | None] = {}
    for column in HISTORY_COLUMNS:
        codes = sorted(GENERAL_CODES if column in GENERAL_COLUMNS else PRIMARY_CODES)
        value = draw(st.sampled_from([_MISSING, None, "", *codes]))
        if value is not _MISSING:
            history[column] = value  # type: ignore[assignment]  # reason: sentinel filtered
    return history


@st.composite
def dated_days(draw: st.DrawFn) -> dict[str, tuple[int, str] | None]:
    return {
        key: draw(
            st.none()
            | st.tuples(st.integers(1, CALENDAR_BY_KEY[key].early_days), st.sampled_from("EA"))
        )
        for key in DATED_KEYS
    }


def _oracle_mode(code: str) -> str:
    return {"E": "early", "A": "absentee"}.get(code[1:], "election_day")


@given(histories(), dated_days())
@example({}, dict.fromkeys(DATED_KEYS))
@example({"GEN24": "VE", "GEN22": "VA"}, dict.fromkeys(DATED_KEYS))
@example({"GEN24": "VE", "GEN22": "VA", "GEN20": "V"}, {"GEN25": (1, "E"), "RO26": (5, "A")})
def test_derived_counts_add_up_and_match_the_codes(
    history: dict[str, str | None], dated: dict[str, tuple[int, str] | None]
) -> None:
    got = derive(history, dated)
    voted = {c: v for c, v in history.items() if v}
    modes = Counter(_oracle_mode(v) for v in voted.values())
    parties = Counter(v[0] for c, v in voted.items() if c not in GENERAL_COLUMNS)
    assert got.n_voted == len(voted)
    assert (got.n_early, got.n_absentee, got.n_election_day) == (
        modes["early"],
        modes["absentee"],
        modes["election_day"],
    )
    assert got.n_early + got.n_absentee + got.n_election_day == got.n_voted
    assert got.n_gen_pres == sum(c in voted for c in PRESIDENTIAL_GENERALS)
    assert got.n_gen_mid == sum(c in voted for c in MIDTERM_GENERALS)
    assert got.n_gen_off == sum(c in voted for c in OFF_YEAR_GENERALS)
    assert got.n_gen_pres + got.n_gen_mid + got.n_gen_off == len(voted.keys() & GENERAL_COLUMNS)
    assert (got.n_primary_r, got.n_primary_d) == (parties["R"], parties["D"])
    assert got.n_primary_r + got.n_primary_d == len(voted.keys() - GENERAL_COLUMNS)

    r, d = got.n_primary_r > 0, got.n_primary_d > 0
    assert (
        got.primary_pattern
        == {
            (True, True): "mixed",
            (True, False): "straight_r",
            (False, True): "straight_d",
            (False, False): "none",
        }[(r, d)]
    )
    p, m = got.n_gen_pres > 0, got.n_gen_mid > 0
    assert (
        got.general_pattern
        == {
            (True, True): "both",
            (True, False): "presidential_only",
            (False, True): "midterm_only",
            (False, False): "none",
        }[(p, m)]
    )

    counts = {mode: modes[mode] for mode in ("early", "absentee", "election_day")}
    top = max(counts.values())
    leaders = [mode for mode, n in counts.items() if n == top]
    expected_mode = None if top == 0 else ("mixed" if len(leaders) > 1 else leaders[0])
    assert got.typical_mode == expected_mode

    if got.n_voted == 0:
        assert got.early_or_absentee_share is None
    else:
        share = (got.n_early + got.n_absentee) / got.n_voted
        assert got.early_or_absentee_share is not None
        assert abs(got.early_or_absentee_share - share) <= 5e-5

    entries = [e for e in dated.values() if e is not None]
    assert got.earliness_n == len(entries)
    if entries:
        assert got.typical_earliness is not None
        assert 0.0 <= got.typical_earliness <= 1.0
    else:
        assert got.typical_earliness is None


@given(histories(), dated_days(), st.randoms(use_true_random=False))
def test_scores_do_not_depend_on_mapping_order(
    history: dict[str, str | None], dated: dict[str, tuple[int, str] | None], rnd: object
) -> None:
    h_items, d_items = list(history.items()), list(dated.items())
    rnd.shuffle(h_items)  # type: ignore[attr-defined]  # reason: st.randoms gives random.Random
    rnd.shuffle(d_items)  # type: ignore[attr-defined]  # reason: st.randoms gives random.Random
    assert derive(dict(h_items), dict(d_items)) == derive(history, dated)


@given(st.integers(2, 60).flatmap(lambda n: st.tuples(st.just(n), st.integers(1, n - 1))))
@example((2, 1))
def test_earliness_runs_from_one_on_the_first_day_to_zero_on_the_last(
    days_and_day: tuple[int, int],
) -> None:
    days, day = days_and_day
    assert earliness(1, days) == 1.0
    assert earliness(days, days) == 0.0
    assert 0.0 <= earliness(day + 1, days) < earliness(day, days) <= 1.0


hash_values = st.lists(st.none() | st.text(max_size=8), max_size=6)


@given(hash_values, hash_values)
@example([None], [""])
@example([""], [])
@example(["ab"], ["a", "b"])
@example(["a", ""], ["", "a"])
@example(["é"], ["é"])
def test_src_hash_fits_int64_and_tells_sequences_apart(
    left: list[str | None], right: list[str | None]
) -> None:
    for values in (left, right):
        assert INT64_MIN <= src_hash(values) <= INT64_MAX
        assert src_hash(values) == src_hash(list(values))
    assert (src_hash(left) == src_hash(right)) == (left == right)


_CHILD = textwrap.dedent(
    """
    import json, sys
    from texas_turnout_scraper.repeat_build import repeat_record
    from texas_turnout_scraper.voter_scores import src_hash
    data = json.load(sys.stdin)
    out = {
        "src": [src_hash(v) for v in data["src"]],
        "repeat": [
            repeat_record(r["vuid"], [tuple(a) for a in r["apps"]], r["removals"]).src_hash
            for r in data["repeat"]
        ],
    }
    json.dump(out, sys.stdout)
    """
)


def _hashes_in_child(payload: str, seed: str) -> dict[str, list[int]]:
    env = {**os.environ, "PYTHONHASHSEED": seed}
    done = subprocess.run(  # noqa: S603  # reason: fixed interpreter and inline script
        [sys.executable, "-c", _CHILD],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        check=True,
        timeout=60,
    )
    return json.loads(done.stdout)


# --------------------------------------------------------------------------- repeat rows

DAY_POOL = (
    "2024-02-29",
    "2025-12-31",
    "2026-01-01",
    "2026-10-20",
    "2026-10-21",
    "2026-11-01",
)
COUNTIES = ("ALPHA", "BRAVO", "ÑANDÚ", 'Q,"X')
vuids = st.integers(0, 10**10 - 1).map(lambda n: str(n).zfill(10))
coords = st.tuples(
    st.sampled_from(DAY_POOL), st.sampled_from(COUNTIES), st.sampled_from(sorted(VOTING_METHODS))
)
appearance_lists = st.lists(coords, max_size=8)
removal_lists = st.lists(st.sampled_from(DAY_POOL), max_size=4)


@given(vuids, appearance_lists, removal_lists)
@example("0000000001", [], [])
@example("0000000001", [("2026-10-21", "ALPHA", "IN-PERSON")] * 2, [])
@example("0000000001", [("2026-10-21", "ALPHA", "MAIL-IN")], ["2026-10-21", "2026-10-21"])
def test_repeat_record_flags_follow_the_appearances(
    vuid: str, appearances: list[tuple[str, str, str]], removals: list[str]
) -> None:
    row = repeat_record(vuid, appearances, removals)
    days = Counter(day for day, _, _ in appearances)
    assert row.vuid == vuid
    assert row.n_counted == len(appearances)
    assert row.n_removed == len(removals)
    assert row.counted_days == sorted(days)
    assert row.removed_days == sorted(set(removals))
    assert row.first_day == (appearances[0][0] if appearances else None)
    assert [(a["day"], a["county"], a["method"]) for a in row.appearances] == appearances
    expected_flags = (
        (FLAG_SAME_DAY if any(n > 1 for n in days.values()) else 0)
        | (FLAG_CROSS_DAY if len(days) > 1 else 0)
        | (FLAG_CROSS_METHOD if len({m for _, _, m in appearances}) > 1 else 0)
        | (FLAG_REMOVED if removals else 0)
    )
    assert row.flags == expected_flags
    assert INT64_MIN <= row.src_hash <= INT64_MAX


@given(st.sampled_from(["SENTINELVUID", "12345", "00000000001", "١" * 10]))
def test_a_bad_vuid_is_refused_naming_the_field_only(vuid: str) -> None:
    with pytest.raises(EarlyVoteSyncError) as raised:
        repeat_record(vuid, [("2026-10-21", "ALPHA", "IN-PERSON")], [])
    assert "vuid" in str(raised.value)
    assert vuid not in str(raised.value)
    assert raised.value.__cause__ is None


def test_a_bad_day_is_refused_naming_the_field_only() -> None:
    with pytest.raises(EarlyVoteSyncError) as raised:
        repeat_record("0000000001", [("SENTINEL-DAY", "SENTINEL-COUNTY", "IN-PERSON")], [])
    assert "SENTINEL" not in str(raised.value)


@settings(max_examples=1)
@example(
    [["\u00e9", None, "", "ab", "e\u0301", "\U0001f600"], [""], [None]],
    [
        (
            "0000000001",
            [("2026-10-21", "ALPHA", "IN-PERSON"), ("2026-10-22", "B\u00c9TA", "MAIL-IN")],
            ["2026-10-23"],
        )
    ],
)
@given(
    st.lists(hash_values, min_size=1, max_size=10),
    st.lists(st.tuples(vuids, appearance_lists, removal_lists), min_size=1, max_size=10),
)
def test_hashes_are_the_same_in_another_process_and_hash_seed(
    src_inputs: list[list[str | None]], repeat_inputs: list[tuple[str, list, list]]
) -> None:
    payload = json.dumps(
        {
            "src": src_inputs,
            "repeat": [{"vuid": v, "apps": a, "removals": r} for v, a, r in repeat_inputs],
        }
    )
    here = {
        "src": [src_hash(v) for v in src_inputs],
        "repeat": [repeat_record(v, a, r).src_hash for v, a, r in repeat_inputs],
    }
    assert _hashes_in_child(payload, "4242") == here


# --------------------------------------------------------------------------- roster keys

# NUL cannot be written by the csv module on Python 3.10, so it is left out of generated cells.
plain_cells = st.text(
    alphabet=st.characters(codec="utf-8", exclude_characters="\ufeff\x00"), max_size=6
)
nasty_cells = st.sampled_from(["", " ", '"', '""', ",", "a,b", "\n", "x\ny", "é", "é"])
cells = plain_cells | nasty_cells
headers = st.lists(
    st.text(alphabet="ABCDEFGHIJ_ ", min_size=1, max_size=6), min_size=1, max_size=4, unique=True
)
data_rows = st.lists(st.lists(cells, min_size=1, max_size=4), max_size=8)


def _csv_bytes(
    header: list[str], rows: list[list[str]], terminator: str, quoting: int = csv.QUOTE_MINIMAL
) -> bytes:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator=terminator, quoting=quoting)
    writer.writerow(header)
    writer.writerows(rows)
    return out.getvalue().encode("utf-8")


@given(headers, data_rows, st.lists(st.integers(0, 7), max_size=4))
@example(["COUNTY"], [[""], [""]], [])
@example(["COUNTY"], [["a,b"], ["x\ny"], ['"']], [0, 0])
def test_line_keys_are_int64_unique_and_count_duplicates(
    header: list[str], rows: list[list[str]], repeats: list[int]
) -> None:
    rows = rows + [rows[i] for i in repeats if i < len(rows)]
    keyed = list(keyed_lines(_csv_bytes(header, rows, "\r\n")))
    assert [cells for _, cells in keyed] == rows
    keys = [key for key, _ in keyed]
    assert len(set(keys)) == len(keys)
    assert all(INT64_MIN <= row_hash <= INT64_MAX for row_hash, _ in keys)
    by_hash: dict[int, list[int]] = {}
    hash_of: dict[tuple[str, ...], int] = {}
    for (row_hash, occ), line in keyed:
        by_hash.setdefault(row_hash, []).append(occ)
        assert hash_of.setdefault(tuple(line), row_hash) == row_hash
    assert len(by_hash) == len(hash_of)
    assert all(occs == list(range(len(occs))) for occs in by_hash.values())


@given(headers, data_rows)
def test_line_keys_ignore_the_bom_and_the_line_ending(
    header: list[str], rows: list[list[str]]
) -> None:
    crlf = list(keyed_lines(_csv_bytes(header, rows, "\r\n")))
    assert list(keyed_lines(_csv_bytes(header, rows, "\n", csv.QUOTE_ALL))) == crlf
    assert list(keyed_lines(b"\xef\xbb\xbf" + _csv_bytes(header, rows, "\r\n"))) == crlf


@given(headers, headers, st.lists(st.lists(cells, min_size=1, max_size=4), min_size=1, max_size=4))
def test_a_header_change_gives_every_line_a_new_key(
    first: list[str], second: list[str], rows: list[list[str]]
) -> None:
    a = {key[0] for key, _ in keyed_lines(_csv_bytes(first, rows, "\r\n"))}
    b = {key[0] for key, _ in keyed_lines(_csv_bytes(second, rows, "\r\n"))}
    assert (a == b) if first == second else a.isdisjoint(b)


@given(st.integers(1, 4096))
@example(1)
@example(2)
@example(3)
@example(KEY_RANGES)
@example(4096)
def test_key_ranges_tile_the_signed_64_bit_space(count: int) -> None:
    ranges = key_ranges(count)
    assert len(ranges) == count
    bounds = [(int(low), int(high)) for low, high in ranges]
    assert [(str(low), str(high)) for low, high in bounds] == list(ranges)
    assert bounds[0][0] == INT64_MIN
    assert bounds[-1][1] == INT64_MAX
    assert all(low <= high for low, high in bounds)
    assert all(prev[1] + 1 == nxt[0] for prev, nxt in zip(bounds, bounds[1:]))


@given(st.integers(1, 64), st.integers(INT64_MIN, INT64_MAX))
@example(16, INT64_MIN)
@example(16, INT64_MAX)
@example(16, 0)
@example(16, -1)
@example(3, INT64_MAX)
def test_every_row_hash_falls_in_exactly_one_range(count: int, row_hash: int) -> None:
    hits = [low for low, high in key_ranges(count) if int(low) <= row_hash <= int(high)]
    assert len(hits) == 1


@given(st.dates(), st.dates())
@example(date(2024, 2, 28), date(2024, 2, 29))
@example(date(2024, 2, 29), date(2024, 3, 1))
@example(date(1900, 2, 28), date(1900, 3, 1))
@example(date(2100, 2, 28), date(2100, 3, 1))
@example(date(2025, 12, 31), date(2026, 1, 1))
@example(date(1, 1, 1), date(9999, 12, 31))
def test_day_number_orders_days_and_counts_them_one_apart(a: date, b: date) -> None:
    na, nb = day_number(a.isoformat()), day_number(b.isoformat())
    assert (na < nb) == (a < b)
    assert nb - na == (b - a).days
    assert date.fromordinal(na) == a


# --------------------------------------------------------------------------- D1 chunks

chunk_lengths = st.sampled_from(
    [0, 1, CHUNK_RAW_BYTES - 1, CHUNK_RAW_BYTES, CHUNK_RAW_BYTES + 1, 2 * CHUNK_RAW_BYTES + 2]
) | st.integers(0, 3 * CHUNK_RAW_BYTES + 3)


@st.composite
def payloads(draw: st.DrawFn) -> bytes:
    length = draw(chunk_lengths)
    pattern = draw(st.binary(min_size=1, max_size=16))
    return (pattern * (length // len(pattern) + 1))[:length]


def _check_chunks(decoded: bytes, size: int) -> None:
    chunks = iter_chunk_b64(decoded)
    raw = [base64.b64decode(chunk, validate=True) for chunk in chunks]
    assert b"".join(raw) == decoded
    assert all(len(piece) == size for piece in raw[:-1])
    assert all(0 < len(piece) <= size for piece in raw)
    assert len(chunks) == -(-len(decoded) // size)
    assert all(len(chunk) < 100_000 for chunk in chunks)


@settings(max_examples=25)
@given(payloads())
def test_chunks_round_trip_at_the_committed_size(decoded: bytes) -> None:
    _check_chunks(decoded, CHUNK_RAW_BYTES)
    assert sha256_decoded(decoded) == hashlib.sha256(decoded).hexdigest()


@given(st.binary(max_size=200), st.integers(1, CHUNK_RAW_BYTES))
@example(b"", 1)
@example(b"\x00", 1)
@example(b"abc", 3)
@example(b"abcd", 3)
def test_chunks_round_trip_at_any_size_under_the_bound(decoded: bytes, size: int) -> None:
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(early_vote_sync, "CHUNK_RAW_BYTES", size)
        _check_chunks(decoded, size)


@given(st.binary(max_size=256))
@example(b"")
def test_sha256_decoded_is_the_hex_sha256(decoded: bytes) -> None:
    digest = sha256_decoded(decoded)
    assert digest == hashlib.sha256(decoded).hexdigest()
    assert len(digest) == 64 and set(digest) <= set("0123456789abcdef")


@given(st.integers(0, 10**13), st.integers(0, 10**13))
@example(0, 1)
@example(2, 3)
@example(3, 4)
def test_estimated_stored_bytes_is_monotonic_and_covers_three_base64_copies(a: int, b: int) -> None:
    low, high = sorted((a, b))
    assert estimated_stored_bytes(low) <= estimated_stored_bytes(high)
    assert estimated_stored_bytes(0) == 0
    for n in (low, high):
        stored = estimated_stored_bytes(n)
        assert stored >= 4 * n
        assert stored == 3 * 4 * -(-n // 3)
        assert estimated_stored_bytes(n + 1) - stored in (0, 12)


@given(st.integers(max_value=-1))
def test_a_negative_length_is_refused(n: int) -> None:
    with pytest.raises(EarlyVoteCapError):
        estimated_stored_bytes(n)


def test_the_cap_admits_the_largest_file_that_fits_and_refuses_one_byte_more() -> None:
    largest = 3 * (D1_DATABASE_CAP_BYTES // 12)
    assert_within_cap(largest, "53815")
    with pytest.raises(EarlyVoteCapError):
        assert_within_cap(largest + 1, "53815")


# --------------------------------------------------------------------------- payload batches

json_scalars = (
    st.none()
    | st.booleans()
    | st.integers(INT64_MIN, INT64_MAX)
    | st.floats(allow_nan=False, allow_infinity=False)
    | st.text(max_size=12)
    | nasty_cells
)
json_rows = st.lists(st.lists(json_scalars, max_size=5), max_size=12)


@given(json_rows, st.integers(1, 10), st.integers(2, 400))
@example([], 1, 2)
@example([["é" * 50]], 1, 10)
@example([[1], [2], [3]], 2, 9)
@example([["x" * 30], ["y"], ["z" * 30]], 10, 40)
def test_payload_batches_respect_both_caps_and_keep_every_row_in_order(
    rows: list[list[object]], batch_rows: int, max_bytes: int
) -> None:
    batches = list(payload_batches(rows, batch_rows=batch_rows, max_bytes=max_bytes))
    decoded = [json.loads(batch) for batch in batches]
    assert [row for batch in decoded for row in batch] == json.loads(json.dumps(rows))
    assert all(1 <= len(batch) <= batch_rows for batch in decoded)
    for text, batch in zip(batches, decoded):
        assert len(text.encode("utf-8")) <= max_bytes or len(batch) == 1


# --------------------------------------------------------------------------- age tiers

MAX_REFERENCE = date(2099, 12, 31)


def _birthday(dob: date, year: int) -> date:
    try:
        return dob.replace(year=year)
    except ValueError:
        return date(year, 3, 1)


def _whole_years(dob: date, on: date) -> int:
    years = on.year - dob.year
    return years - 1 if _birthday(dob, on.year) > on else years


def _tier(age: int) -> str | None:
    labels = [label for low, high, label, _ in AGE_TIERS if low <= age <= high]
    assert len(labels) <= 1
    return labels[0] if labels else None


@st.composite
def birth_and_reference(draw: st.DrawFn) -> tuple[date, date]:
    dob = draw(st.dates(date(1880, 1, 1), date(2080, 12, 31)))
    kind = draw(st.sampled_from(["birthday", "day_before", "day_after", "any"]))
    if kind == "any":
        on = dob + timedelta(days=draw(st.integers(0, 120 * 366)))
    else:
        shift = {"birthday": 0, "day_before": -1, "day_after": 1}[kind]
        on = _birthday(dob, dob.year + draw(st.integers(0, 120))) + timedelta(days=shift)
    return dob, min(max(on, dob), MAX_REFERENCE)


def _age_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    for sql in VOTER_SCHEMA_CREATES:
        conn.execute(sql)
    for sql, params in seed_statements():
        conn.execute(sql, params)
    return conn


def _insert_voter(conn: sqlite3.Connection, vuid: str, dob: date) -> None:
    raw: dict[str, str | None] = dict.fromkeys(VOTERFILE_COLUMNS)
    raw.update({"VUID": vuid, "SEX": "U", "DOB": dob.strftime("%Y%m%d")})
    voter = parse_voter(raw, {}, today=date(2100, 1, 1))
    assert isinstance(voter, Voter)
    conn.execute(VOTER_UPSERT_SQL, [json.dumps([voter_row(voter)])])


@given(st.lists(birth_and_reference(), min_size=1, max_size=6))
@example([(date(2008, 2, 29), date(2026, 2, 28))])
@example([(date(2008, 2, 29), date(2026, 3, 1))])
@example([(date(2008, 2, 29), date(2028, 2, 29))])
@example([(date(2008, 11, 3), date(2026, 11, 2)), (date(2008, 11, 3), date(2026, 11, 3))])
@example([(date(2002, 11, 4), date(2026, 11, 3)), (date(1941, 11, 3), date(2026, 11, 3))])
@example([(date(1880, 1, 1), date(2080, 1, 1))])
@example([(date(2000, 12, 31), date(2025, 1, 1)), (date(2001, 1, 1), date(2025, 12, 31))])
def test_election_day_age_and_tier_are_whole_years_with_the_march_first_rule(
    cases: list[tuple[date, date]],
) -> None:
    conn = _age_db()
    for index, (dob, on) in enumerate(cases):
        vuid = str(index + 1).zfill(10)
        _insert_voter(conn, vuid, dob)
        conn.execute(
            "UPDATE early_vote_election_calendar SET election_date = ? WHERE is_current = 1",
            [on.isoformat()],
        )
        got = conn.execute(
            "SELECT age_election_day, age_tier_election_day FROM early_vote_voter_age "
            "WHERE vuid = ?",
            [vuid],
        ).fetchone()
        age = _whole_years(dob, on)
        assert got == (age, _tier(age))
    conn.close()


@given(st.integers(-5, 250))
def test_age_tiers_cover_18_to_200_once_and_nothing_else(age: int) -> None:
    matches = [label for low, high, label, _ in AGE_TIERS if low <= age <= high]
    assert len(matches) == (1 if 18 <= age <= 200 else 0)


# --------------------------------------------------------------------------- geography

ascii_names = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ .'-", max_size=12
)


@given(st.text(max_size=16))
@example("DE WITT")
@example("ß")
@example("İ")
@example("ﬁ")
def test_county_key_is_idempotent_and_ascii_upper_letters(name: str) -> None:
    key = county_key(name)
    assert county_key(key) == key
    assert all("A" <= ch <= "Z" for ch in key)


@given(ascii_names)
@example("De Witt")
@example("DEWITT")
@example("de-witt.")
def test_county_key_ignores_case_spaces_and_punctuation_for_ascii_names(name: str) -> None:
    letters = "".join(ch for ch in name if ch.isalpha()).upper()
    assert county_key(name) == county_key(name.lower()) == county_key(name.upper()) == letters


fips_codes = st.sampled_from(["48001", "48003", "48201"])
precinct_numbers = st.integers(0, 9999).map(str)


@st.composite
def precinct_keys(draw: st.DrawFn) -> dict[tuple[str, str], str]:
    pairs = draw(st.lists(st.tuples(fips_codes, precinct_numbers), max_size=8, unique=True))
    return {(fips, prec.zfill(4)): f"{fips}{prec.zfill(4)}" for fips, prec in pairs}


@given(precinct_keys(), st.none() | fips_codes, st.sampled_from(["", " ", "\t"]) | precinct_numbers)
@example({("48001", "0001"): "480010001"}, "48001", "1")
@example({("48001", "0001"): "480010001"}, "48003", "1")
@example({("48001", "0001"): "480010001"}, None, "0001")
@example({("48001", "0001"): "480010001"}, "48001", " ")
def test_resolve_precinct_matches_only_in_its_county_and_ignores_padding(
    keys: dict[tuple[str, str], str], fips: str | None, precinct: str
) -> None:
    pctkey, rule = resolve_precinct(fips, precinct, keys)
    assert rule in ALIAS_RULES
    if fips is None or not precinct.strip():
        assert (pctkey, rule) == (None, "unmatched")
        return
    expected = keys.get((fips, precinct.strip().zfill(4)))
    assert pctkey == expected
    assert pctkey is None or any(k[0] == fips and v == pctkey for k, v in keys.items())
    assert (rule == "unmatched") == (expected is None)
    for spelled in (precinct.strip().zfill(4), f" {precinct} "):
        assert resolve_precinct(fips, spelled, keys)[0] == expected


@given(precinct_keys(), fips_codes, st.integers(0, 999).map(str))
@example({}, "48001", "7")
def test_an_exact_key_wins_over_a_padded_one(
    keys: dict[tuple[str, str], str], fips: str, precinct: str
) -> None:
    keys = {**keys, (fips, precinct): "EXACT", (fips, precinct.zfill(4)): "PADDED"}
    assert resolve_precinct(fips, precinct, keys) == ("EXACT", "exact")
    assert resolve_precinct(fips, precinct.zfill(4), keys) == ("PADDED", "exact")
