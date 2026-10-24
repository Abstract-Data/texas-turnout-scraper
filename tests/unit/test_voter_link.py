"""The local voter link: target VUIDs from D1, the voterfile through DuckDB, dated early days."""

from __future__ import annotations

import csv
import io
import json
import logging
import re
import sqlite3
import zipfile
from collections.abc import Sequence
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from texas_turnout_scraper.early_vote_roster_rows import ROW_TABLE_SQL, VERSION_TABLE_SQL
from texas_turnout_scraper.models import CivixElection
from texas_turnout_scraper.voter_link import (
    load_dated_history,
    read_voterfile,
    run_voter_link_on_store,
    voterfile_sql,
)
from texas_turnout_scraper.voter_tables import VOTER_SCHEMA_CREATES, VOTERFILE_COLUMNS

TODAY = date(2026, 10, 24)
EXTRA_COLUMNS = ("COUNTY", "PCT", "CDPLANC2333", "SD2022", "HD2022", "LASTNAME", "ADDRESS")
HEADER = ("LASTNAME", "VUID", "COUNTY", "SEX", "PCT", "DOB", "ADDRESS", "EDR", "CDPLANC2333")


def _header() -> list[str]:
    history = [c for c in VOTERFILE_COLUMNS if c not in HEADER]
    return [*HEADER, "SD2022", "HD2022", *reversed(history)]


def _person(vuid: str, **cells: str) -> dict[str, str]:
    row = dict.fromkeys(_header(), "")
    row.update(
        {
            "VUID": vuid,
            "SEX": "M",
            "DOB": "19700101",
            "EDR": "19900101",
            "LASTNAME": f"FAKENAME{vuid[-3:]}",
            "ADDRESS": "1 FAKE ST",
            "COUNTY": "TRAVIS",
            "PCT": "0101",
            "CDPLANC2333": "10",
            "SD2022": "14",
            "HD2022": "49",
        }
    )
    row.update(cells)
    return row


PEOPLE = [
    _person("1000000001", GEN24="VE", PRI26="RE"),
    _person("1000000002", GEN22="V", PRI24="D"),
    _person("1000000003", GEN24="BAD"),
    _person("1000000004", DOB="00010101"),
    _person("1000000005", EDR="98640101", RO26="DA"),
    _person("2000000099", GEN24="V"),
]
TARGETS = ["1000000001", "1000000002", "1000000003", "1000000004", "1000000005", "1000000777"]


def _write_voterfile(path: Path, people: Sequence[dict[str, str]]) -> Path:
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=_header())
        writer.writeheader()
        writer.writerows(people)
    return path


class _Store:
    def __init__(self, *, voter_tables: bool = True) -> None:
        self.conn = sqlite3.connect(":memory:")
        for sql in (ROW_TABLE_SQL, VERSION_TABLE_SQL):
            self.conn.execute(sql)
        if voter_tables:
            for sql in VOTER_SCHEMA_CREATES:
                self.conn.execute(sql)
        self.statements: list[tuple[str, list[str]]] = []
        self.schema_calls = 0

    def _database_uuid_if_exists(self, election_id: str) -> str | None:
        return "db"

    def _ensure_schema(self, database_id: str) -> None:
        self.schema_calls += 1
        for sql in VOTER_SCHEMA_CREATES:
            self.conn.execute(sql)

    def _query_rows(self, database_id: str, sql: str, params: list[str]) -> list[dict[str, Any]]:
        cur = self.conn.execute(sql, params)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]

    def _statements(self, database_id: str, statements: list[tuple[str, list[str]]]) -> None:
        for sql, params in statements:
            self.statements.append((sql, list(params)))
            self.conn.execute(sql, params)

    def rows(self, sql: str) -> list[tuple[Any, ...]]:
        return self.conn.execute(sql).fetchall()


def _roster(store: _Store, vuids: Sequence[str], *, removed: Sequence[str] = ()) -> None:
    for ev_date, day in (("2026-10-20", 739909), ("2026-10-21", 739910)):
        store.conn.execute(
            "INSERT INTO early_vote_roster_version VALUES ('53815', ?, 1, 'f', 's', 0, 0)",
            (ev_date,),
        )
        for index, vuid in enumerate([*vuids, *removed]):
            store.conn.execute(
                "INSERT INTO early_vote_roster_row (county, voter_name, id_voter, voting_method, "
                "precinct, day, row_hash, occ, extra, added_in, removed_in) "
                "VALUES ('TRAVIS', 'N', ?, 'IN-PERSON', '1', ?, ?, 0, '{}', 1, ?)",
                (vuid, day, index * 2**58 - 2**62, 1 if vuid in removed else None),
            )


@pytest.fixture
def voterfile(tmp_path: Path) -> Path:
    return _write_voterfile(tmp_path / "voterfile.csv", PEOPLE)


@pytest.fixture
def store() -> _Store:
    s = _Store()
    _roster(s, TARGETS, removed=["2000000099"])
    return s


def _run(store: Any, voterfile: Path, **kwargs: Any) -> Any:
    kwargs.setdefault("dry_run", False)
    return run_voter_link_on_store(store, "53815", voterfile, today=TODAY, **kwargs)


@pytest.mark.parametrize("dry_run", [False, True])
def test_a_voterfile_that_repeats_a_vuid_stops_the_link_before_the_meta_is_written(
    store: _Store, tmp_path: Path, dry_run: bool
) -> None:
    from texas_turnout_scraper.early_vote_errors import EarlyVoteSyncError

    people = [*PEOPLE, _person("1000000001", GEN22="VE")]
    repeated = _write_voterfile(tmp_path / "repeats.csv", people)
    with pytest.raises(EarlyVoteSyncError, match="repeats a VUID") as excinfo:
        _run(store, repeated, dry_run=dry_run)
    assert "1000000001" not in str(excinfo.value)
    assert store.rows("SELECT name FROM early_vote_voter_meta") == []


def test_the_query_selects_exactly_the_21_columns() -> None:
    sql = voterfile_sql()
    select_list = sql.split(" FROM ", 1)[0].removeprefix("SELECT ")
    assert [c.strip() for c in select_list.split(",")] == [f'"{c}"' for c in VOTERFILE_COLUMNS]
    for excluded in EXTRA_COLUMNS:
        assert excluded not in sql


def test_only_target_vuids_are_read_from_the_voterfile(voterfile: Path) -> None:
    rows = {str(r["VUID"]): r for r in read_voterfile(voterfile, {"1000000002", "1000000004"})}
    assert sorted(rows) == ["1000000002", "1000000004"]
    assert all(set(r) == set(VOTERFILE_COLUMNS) for r in rows.values())
    assert (rows["1000000002"]["PRI24"], rows["1000000004"]["DOB"]) == ("D", "00010101")
    assert rows["1000000002"]["GEN24"] is None


def test_an_apply_stores_matched_voters_and_rejects_and_writes_meta_last(
    store: _Store, voterfile: Path
) -> None:
    result = _run(store, voterfile)
    assert (result.targets, result.matched, result.unmatched) == (6, 5, 1)
    assert result.matched_share == pytest.approx(5 / 6)
    assert (result.rejects, result.invalid_dob, result.invalid_edr) == (1, 1, 1)
    assert (result.written, result.unchanged) == (4, 0)
    assert store.rows("SELECT vuid FROM early_vote_voter ORDER BY vuid") == [
        ("1000000001",),
        ("1000000002",),
        ("1000000004",),
        ("1000000005",),
    ]
    assert store.rows(
        "SELECT sex, dob, edr, flags, gen24, pri26, n_voted, primary_pattern "
        "FROM early_vote_voter WHERE vuid = '1000000001'"
    ) == [("M", "1970-01-01", "1990-01-01", 0, "VE", "RE", 2, "straight_r")]
    assert store.rows("SELECT dob, flags FROM early_vote_voter WHERE vuid = '1000000004'") == [
        (None, 1)
    ]
    ((vuid, reason, raw),) = store.rows("SELECT vuid, reason, raw FROM early_vote_voter_reject")
    assert (vuid, reason.split(":")[0]) == ("1000000003", "gen24")
    assert set(json.loads(raw)) == set(VOTERFILE_COLUMNS)
    assert "early_vote_voter_meta" in store.statements[-1][0]
    assert all("early_vote_voter_meta" not in sql for sql, _ in store.statements[:-1])
    meta = dict(store.rows("SELECT name, value FROM early_vote_voter_meta"))
    assert json.loads(meta["voterfile"])["name"] == "voterfile.csv"
    assert meta["score_version"] == "1"
    assert store.rows("SELECT COUNT(*) FROM early_vote_election_calendar") == [(4,)]
    assert store.rows("SELECT COUNT(*) FROM age_tier") == [(7,)]


def test_columns_outside_the_selection_never_reach_d1(store: _Store, voterfile: Path) -> None:
    _run(store, voterfile)
    sent = json.dumps(store.statements)
    for value in ("FAKENAME", "1 FAKE ST", "TRAVIS"):
        assert value not in sent


def test_a_second_apply_writes_nothing(store: _Store, voterfile: Path) -> None:
    _run(store, voterfile)
    before = len(store.statements)
    result = _run(store, voterfile)
    assert (result.status, result.written, result.unchanged) == ("unchanged", 0, 4)
    assert len(store.statements) == before


def test_a_changed_history_field_updates_exactly_that_voter(
    store: _Store, tmp_path: Path, voterfile: Path
) -> None:
    _run(store, voterfile)
    changed = [*PEOPLE]
    changed[1] = _person("1000000002", GEN22="VE", PRI24="D")
    _run(store, _write_voterfile(voterfile, changed))
    start = len(store.statements)
    edited = [*changed]
    edited[0] = _person("1000000001", GEN24="VA", PRI26="RE")
    result = _run(store, _write_voterfile(voterfile, edited))
    assert (result.written, result.unchanged) == (1, 3)
    upserts = [
        json.loads(params[0])
        for sql, params in store.statements[start:]
        if sql.startswith("INSERT INTO early_vote_voter ")
    ]
    assert [[row[0] for row in batch] for batch in upserts] == [["1000000001"]]
    assert store.rows("SELECT gen24 FROM early_vote_voter WHERE vuid = '1000000001'") == [("VA",)]


def test_a_reject_that_becomes_valid_leaves_the_reject_table(
    store: _Store, voterfile: Path
) -> None:
    _run(store, voterfile)
    fixed = [*PEOPLE]
    fixed[2] = _person("1000000003", GEN24="V")
    result = _run(store, _write_voterfile(voterfile, fixed))
    assert result.rejects == 0
    assert store.rows("SELECT vuid FROM early_vote_voter_reject") == []
    assert store.rows("SELECT gen24 FROM early_vote_voter WHERE vuid = '1000000003'") == [("V",)]


def test_writes_are_bounded(store: _Store, voterfile: Path) -> None:
    _run(store, voterfile, batch_rows=2)
    batches = [
        json.loads(p[0])
        for sql, p in store.statements
        if sql.startswith("INSERT INTO early_vote_voter ")
    ]
    assert sum(len(b) for b in batches) == 4
    assert max(len(b) for b in batches) == 2


def test_a_dry_run_on_a_fresh_database_creates_and_writes_nothing_and_reports(
    voterfile: Path,
) -> None:
    fresh = _Store(voter_tables=False)
    _roster(fresh, TARGETS)
    result = _run(fresh, voterfile, dry_run=True)
    assert (fresh.schema_calls, fresh.statements) == (0, [])
    assert fresh.rows("SELECT name FROM sqlite_master WHERE name LIKE 'early_vote_voter%'") == []
    assert (result.status, result.targets, result.matched, result.rejects) == ("dry_run", 6, 5, 1)
    assert (result.written, result.unchanged) == (0, 0)


def test_a_dry_run_after_an_apply_reports_unchanged_and_writes_nothing(
    store: _Store, voterfile: Path
) -> None:
    _run(store, voterfile)
    calls, before = store.schema_calls, len(store.statements)
    result = _run(store, voterfile, dry_run=True)
    assert (store.schema_calls, len(store.statements)) == (calls, before)
    assert result.unchanged == 4


def test_logs_and_rejects_carry_no_voter_values(
    store: _Store, voterfile: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        _run(store, voterfile)
    assert "matched=5" in caplog.text
    for value in ("10000000", "FAKENAME", "19700101", "1970-01-01", "BAD"):
        assert value not in caplog.text
    reasons = " ".join(r for (r,) in store.rows("SELECT reason FROM early_vote_voter_reject"))
    assert "BAD" not in reasons


# --- dated history -------------------------------------------------------------------------

DATED = {
    51031: ("11/04/2025", date(2025, 10, 20), 12),
    53813: ("03/03/2026", date(2026, 2, 17), 11),
    53814: ("03/03/2026", date(2026, 2, 17), 11),
    58314: ("05/26/2026", date(2026, 5, 18), 5),
    58315: ("05/26/2026", date(2026, 5, 18), 5),
    53815: ("11/03/2026", date(2026, 10, 14), 17),
}


def _election(civix_id: int, days: int | None = None) -> CivixElection:
    election_date, first, count = DATED[civix_id]
    count = count if days is None else days
    return CivixElection.model_validate(
        {
            "source_election_id": str(civix_id),
            "id": civix_id,
            "type": "EV",
            "election_date": election_date,
            "election_name": f"Election {civix_id}",
            "certified": True,
            "early_voting_dates": [
                {"date": first + timedelta(days=k - 1), "date_turnout_id": k}
                for k in range(1, count + 1)
            ],
            "counties": [],
        }
    )


def _day_csv(rows: Sequence[tuple[str, str]]) -> bytes:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["COUNTY", "VOTER_NAME", "ID_VOTER", "VOTING_METHOD", "PRECINCT"])
    for vuid, method in rows:
        writer.writerow(["TRAVIS", "FAKE PERSON", vuid, method, "1"])
    return out.getvalue().encode()


def _zipped(body: bytes) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("STATEWIDE.csv", body)
    return out.getvalue()


class _Fetcher:
    def __init__(self, *, short_runoff: bool = False) -> None:
        self.fetches: list[tuple[int, date]] = []
        self.short_runoff = short_runoff
        self.files: dict[tuple[int, int], bytes] = {
            (53813, 2): _day_csv([("1000000001", "IN-PERSON"), ("2000000099", "IN-PERSON")]),
            (53813, 5): _day_csv([("1000000001", "MAIL-IN")]),
            (53814, 1): _day_csv([("1000000002", "MAIL-IN")]),
            (58314, 3): _zipped(_day_csv([("1000000005", "MAIL-IN")])),
            (51031, 12): _day_csv([("1000000004", "IN-PERSON"), ("1000000001", "MAIL-IN")]),
        }

    def list_elections(self) -> list[CivixElection]:
        runoff = 4 if self.short_runoff else None
        return [
            _election(51031),
            _election(53813),
            _election(53814),
            _election(58314, runoff),
            _election(58315, runoff),
            _election(53815),
        ]

    def fetch_statewide(self, election_id: int, election_date: date) -> bytes:
        self.fetches.append((election_id, election_date))
        first = DATED[election_id][1]
        k = (election_date - first).days + 1
        return self.files.get((election_id, k), _day_csv([]))


def test_dated_history_keeps_day_and_mode_for_targets_only(tmp_path: Path) -> None:
    fetcher = _Fetcher()
    history = load_dated_history(fetcher, set(TARGETS), tmp_path / "cache")
    assert history.days["PRI26"] == {"1000000001": (2, "E"), "1000000002": (1, "A")}
    assert history.days["RO26"] == {"1000000005": (3, "A")}
    assert history.days["GEN25"] == {"1000000004": (12, "E"), "1000000001": (12, "A")}
    assert history.found == {"GEN25": 2, "PRI26": 2, "RO26": 1}
    assert all("2000000099" not in days for days in history.days.values())
    assert len(fetcher.fetches) == 12 + 11 + 11 + 5 + 5


def test_a_rerun_resumes_from_the_cache_without_fetching(tmp_path: Path) -> None:
    load_dated_history(_Fetcher(), set(TARGETS), tmp_path / "cache")
    again = _Fetcher()
    history = load_dated_history(again, set(TARGETS), tmp_path / "cache")
    assert again.fetches == []
    assert history.found == {"GEN25": 2, "PRI26": 2, "RO26": 1}


def test_a_calendar_mismatch_warns_with_counts_only(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        load_dated_history(_Fetcher(short_runoff=True), set(TARGETS), tmp_path / "cache")
    assert "RO26" in caplog.text
    assert "1000000005" not in caplog.text


def test_an_apply_with_dated_history_stores_days_and_earliness(
    store: _Store, voterfile: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO):
        result = _run(
            store,
            voterfile,
            fetcher=_Fetcher(),
            cache_dir=tmp_path / "cache",
        )
    assert result.dated_found == {"GEN25": 2, "PRI26": 2, "RO26": 1}
    assert store.rows(
        "SELECT pri26_day, gen25_day, gen25_mode, typical_earliness, earliness_n "
        "FROM early_vote_voter WHERE vuid = '1000000001'"
    ) == [(2, 12, "A", 0.45, 2)]
    assert store.rows(
        "SELECT ro26_day, pri26_day FROM early_vote_voter WHERE vuid = '1000000005'"
    ) == [(3, None)]
    assert not re.search(r"\b1000000\d{3}\b", caplog.text)


# --- streaming -----------------------------------------------------------------------------

_CODES = ("", "V", "VE", "VA")
_PRIMARY = ("", "R", "D", "RE", "DE", "RA", "DA")


def _many(path: Path, count: int) -> list[str]:
    """A synthetic voterfile of ``count`` valid voters with varied history; returns the VUIDs."""
    header = _header()
    vuids = [f"{3_000_000_000 + n:010d}" for n in range(count)]
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        for n, vuid in enumerate(vuids):
            row = _person(
                vuid,
                DOB=f"19{40 + n % 60:02d}0{1 + n % 9}1{n % 9}",
                GEN24=_CODES[n % 4],
                GEN20=_CODES[n // 4 % 4],
                PRI26=_PRIMARY[n % 7],
                RO24=_PRIMARY[n // 7 % 7],
            )
            writer.writerow([row[c] for c in header])
    return vuids


class _CountingStore:
    """Answers the link's reads from a VUID list and keeps only the size of each write."""

    def __init__(self, vuids: Sequence[str], *, stored_every: int = 2) -> None:
        from texas_turnout_scraper.early_vote_roster_rows import key_ranges
        from texas_turnout_scraper.voter_link import TARGET_KEY_RANGES

        self.vuids = sorted(vuids)
        # Every ``stored_every``-th voter is already in D1 with a hash that no longer matches.
        self.stored = self.vuids[::stored_every]
        self.first_low = key_ranges(TARGET_KEY_RANGES)[0][0]
        self.writes: list[tuple[str, int, int]] = []
        self.consumed = 0

    def _database_uuid_if_exists(self, election_id: str) -> str | None:
        return "db"

    def _ensure_schema(self, database_id: str) -> None:
        return None

    def _query_rows(self, database_id: str, sql: str, params: list[str]) -> list[dict[str, Any]]:
        if "early_vote_roster_version" in sql:
            return [{"ev_date": "2026-10-20"}]
        if "FROM early_vote_roster_row" in sql:
            if params[1] != self.first_low:
                return []
            return [{"id_voter": vuid} for vuid in self.vuids]
        if "CAST(src_hash AS TEXT)" in sql:
            low, high = params
            return [{"vuid": v, "src_hash": "-1"} for v in self.stored if low <= v <= high]
        return []

    def _statements(self, database_id: str, statements: list[tuple[str, list[str]]]) -> None:
        for sql, params in statements:
            self.writes.append((sql, len(json.loads(params[0])), self.consumed))


def _spy_on_reads(monkeypatch: pytest.MonkeyPatch, store: _CountingStore) -> None:
    import texas_turnout_scraper.voter_link as voter_link

    original = voter_link.read_voterfile

    def counting(path: Path, targets: Any) -> Any:
        for row in original(path, targets):
            store.consumed += 1
            yield row

    monkeypatch.setattr(voter_link, "read_voterfile", counting)


def _peak(tmp_path: Path, count: int) -> tuple[int, Any]:
    import gc
    import tracemalloc

    vuids = _many(tmp_path / f"voters{count}.csv", count)
    store = _CountingStore(vuids)
    gc.collect()
    tracemalloc.start()
    try:
        result = _run(store, tmp_path / f"voters{count}.csv", batch_rows=1000)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert (result.status, result.matched, result.written) == ("wrote", count, count)
    return peak, store


def test_memory_does_not_grow_with_the_changed_voters(tmp_path: Path) -> None:
    small, _ = _peak(tmp_path, 20_000)
    large, store = _peak(tmp_path, 60_000)
    assert large / 60_000 < 1_500, f"peak {large / 60_000:.0f} bytes per voter"
    # The target set, the sorted targets and the stored hashes grow by design; nothing else may.
    assert large < 2.5 * small + 4_000_000, f"peak {large} for 60k against {small} for 20k"
    # Measured about 120 bytes per extra voter; a retained Voter costs about 4,700.
    assert (large - small) / 40_000 < 400, f"{(large - small) / 40_000:.0f} bytes per extra voter"
    voter_writes = [
        n for sql, n, _ in store.writes if sql.startswith("INSERT INTO early_vote_voter ")
    ]
    assert sum(voter_writes) == 60_000


def test_writes_flush_while_the_voterfile_streams_and_never_exceed_batch_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "voters.csv"
    store = _CountingStore(_many(path, 5_000))
    _spy_on_reads(monkeypatch, store)
    result = _run(store, path, batch_rows=1000)
    voter_writes = [
        (n, at) for sql, n, at in store.writes if sql.startswith("INSERT INTO early_vote_voter ")
    ]
    assert result.written == sum(n for n, _ in voter_writes) == 5_000
    assert max(n for n, _ in voter_writes) <= 1000
    assert voter_writes[0][1] < 5_000, "first voter flush waited for the whole voterfile"
    assert "early_vote_election_calendar" in store.writes[0][0]
    assert "early_vote_voter_meta" in store.writes[-1][0]
    assert all("early_vote_voter_meta" not in sql for sql, _, _ in store.writes[:-1])


def test_a_dry_run_streams_and_counts_without_writing(tmp_path: Path) -> None:
    path = tmp_path / "voters.csv"
    store = _CountingStore(_many(path, 3_000), stored_every=3)
    result = _run(store, path, dry_run=True, batch_rows=1000)
    assert store.writes == []
    assert (result.status, result.written, result.pending, result.unchanged) == (
        "dry_run",
        0,
        3_000,
        0,
    )
