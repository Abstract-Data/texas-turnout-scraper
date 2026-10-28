"""Repeat-VUID flags: one row per voter counted more than once or removed, and excess counts."""

from __future__ import annotations

import json
import logging
import os
import random
import re
import sqlite3
import subprocess
import sys
import textwrap
from collections.abc import Sequence
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.dialects import sqlite
from sqlalchemy.schema import CreateTable
from typing_extensions import override

from texas_turnout_scraper.early_vote_day_changes import DAY_CHANGE_TABLE_SQL
from texas_turnout_scraper.early_vote_errors import EarlyVoteSyncError
from texas_turnout_scraper.early_vote_roster_rows import (
    ROW_TABLE_SQL,
    VERSION_TABLE_SQL,
    day_number,
)
from texas_turnout_scraper.early_vote_sync import _SCHEMA_CREATES
from texas_turnout_scraper.repeat_build import run_repeat_build_on_store
from texas_turnout_scraper.repeat_models import RepeatExcessRow, RepeatMeta, VuidRepeatRow
from texas_turnout_scraper.repeat_tables import (
    EXCESS_TABLE_SQL,
    FLAG_CROSS_DAY,
    FLAG_CROSS_METHOD,
    FLAG_REMOVED,
    FLAG_SAME_DAY,
    META_TABLE_SQL,
    REPEAT_COLUMNS,
    REPEAT_SCHEMA_CREATES,
    REPEAT_TABLE_SQL,
)

ELECTION = "53815"
D1, D2, D3 = "2026-10-20", "2026-10-21", "2026-10-24"


class _Store:
    """The roster tables on sqlite with the real DDL; repeat tables only after the schema step."""

    def __init__(self, *, repeat_tables: bool = True) -> None:
        self.conn = sqlite3.connect(":memory:")
        for sql in (ROW_TABLE_SQL, VERSION_TABLE_SQL, DAY_CHANGE_TABLE_SQL):
            self.conn.execute(sql)
        if repeat_tables:
            for sql in REPEAT_SCHEMA_CREATES:
                self.conn.execute(sql)
        self.statements: list[tuple[str, list[str]]] = []
        self.schema_calls = 0
        self.next_hash: dict[str, int] = {}

    def _database_uuid_if_exists(self, election_id: str) -> str | None:
        return "db"

    def _ensure_schema(self, database_id: str) -> None:
        self.schema_calls += 1
        for sql in REPEAT_SCHEMA_CREATES:
            self.conn.execute(sql)

    def _query_rows(self, database_id: str, sql: str, params: list[str]) -> list[dict[str, Any]]:
        cur = self.conn.execute(sql, params)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]

    def _statements(self, database_id: str, statements: list[tuple[str, list[str]]]) -> None:
        for sql, params in statements:
            self.statements.append((sql, list(params)))
            self.conn.execute(sql, params)

    def rows(self, sql: str, params: Sequence[object] = ()) -> list[tuple[Any, ...]]:
        return self.conn.execute(sql, params).fetchall()

    def add(
        self,
        ev_date: str,
        vuid: str,
        method: str = "IN-PERSON",
        county: str = "TRAVIS",
        *,
        row_hash: int | None = None,
        removed_in: int | None = None,
    ) -> None:
        """One roster row. Without ``row_hash`` rows of a day take increasing hashes."""
        self.conn.execute(
            "INSERT OR IGNORE INTO early_vote_roster_version VALUES (?, ?, 1, 'f', 's', 0, 0)",
            (ELECTION, ev_date),
        )
        if row_hash is None:
            row_hash = self.next_hash.get(ev_date, -(2**62))
            self.next_hash[ev_date] = row_hash + 2**40
        self.conn.execute(
            "INSERT INTO early_vote_roster_row (county, voter_name, id_voter, voting_method, "
            "precinct, day, row_hash, occ, extra, added_in, removed_in) "
            "VALUES (?, 'FAKE PERSON', ?, ?, '1', ?, ?, 0, '{}', 1, ?)",
            (county, vuid, method, day_number(ev_date), row_hash, removed_in),
        )

    def bump(self, ev_date: str) -> None:
        """A new roster version for the day, as a reload that changed the file writes."""
        self.conn.execute(
            "UPDATE early_vote_roster_version SET version = version + 1, sha256 = sha256 || 'x' "
            "WHERE ev_date = ?",
            (ev_date,),
        )

    def removal(self, ev_date: str, vuid: str, new_sha: str = "n1") -> None:
        self.conn.execute(
            "INSERT INTO early_vote_day_change VALUES (?, ?, 'o', ?, 'removed', ?, 'TRAVIS', "
            "'IN-PERSON', ?)",
            (ELECTION, ev_date, new_sha, vuid, f"{ev_date}T12:00:00Z:{new_sha}"),
        )

    def repeat(self, vuid: str) -> dict[str, Any] | None:
        rows = self._query_rows("db", "SELECT * FROM early_vote_vuid_repeat WHERE vuid = ?", [vuid])
        if not rows:
            return None
        row = rows[0]
        for column in ("counted_days", "appearances", "removed_days"):
            row[column] = json.loads(row[column])
        return row

    def excess(self) -> dict[tuple[str, str, str], int]:
        return {
            (d, c, m): n
            for d, c, m, n in self.rows(
                "SELECT ev_date, county, voting_method, n FROM early_vote_repeat_excess"
            )
        }

    def meta(self) -> dict[str, str]:
        return dict(self.rows("SELECT name, value FROM early_vote_repeat_meta"))


def _run(store: Any, **kwargs: Any) -> Any:
    kwargs.setdefault("dry_run", False)
    return run_repeat_build_on_store(store, ELECTION, **kwargs)


def _v(n: int) -> str:
    return f"{1_000_000_000 + n:010d}"


@pytest.fixture
def store() -> _Store:
    return _Store()


def test_a_voter_seen_once_and_never_removed_has_no_row(store: _Store) -> None:
    store.add(D1, _v(1))
    store.add(D2, _v(2))
    result = _run(store)
    assert store.rows("SELECT vuid FROM early_vote_vuid_repeat") == []
    assert store.excess() == {}
    assert (result.raw_rows, result.distinct_voters, result.repeat_voters) == (2, 2, 0)


def test_a_same_day_repeat_lists_both_appearances_and_counts_one_excess(store: _Store) -> None:
    store.add(D1, _v(1), county="TRAVIS", row_hash=-5)
    store.add(D1, _v(1), county="HAYS", row_hash=7)
    _run(store)
    row = store.repeat(_v(1))
    assert row is not None
    assert (row["n_counted"], row["n_removed"], row["first_day"]) == (2, 0, D1)
    assert row["counted_days"] == [D1]
    assert row["appearances"] == [
        {"day": D1, "method": "IN-PERSON", "county": "TRAVIS"},
        {"day": D1, "method": "IN-PERSON", "county": "HAYS"},
    ]
    assert row["removed_days"] == []
    assert row["flags"] == FLAG_SAME_DAY
    assert store.excess() == {(D1, "HAYS", "IN-PERSON"): 1}


def test_within_a_day_the_lowest_row_key_is_the_first_appearance(store: _Store) -> None:
    # Both keys fall in one of the build's row-hash ranges, so the read order decides.
    store.add(D1, _v(1), county="HAYS", row_hash=900)
    store.add(D1, _v(1), county="TRAVIS", row_hash=100)
    _run(store)
    row = store.repeat(_v(1))
    assert row is not None
    assert [a["county"] for a in row["appearances"]] == ["TRAVIS", "HAYS"]
    assert store.excess() == {(D1, "HAYS", "IN-PERSON"): 1}


def test_a_cross_day_repeat_is_attributed_to_the_earliest_day(store: _Store) -> None:
    store.add(D3, _v(1), row_hash=-(2**62))
    store.add(D1, _v(1), row_hash=2**62)
    _run(store)
    row = store.repeat(_v(1))
    assert row is not None
    assert (row["first_day"], row["counted_days"], row["flags"]) == (D1, [D1, D3], FLAG_CROSS_DAY)
    assert [a["day"] for a in row["appearances"]] == [D1, D3]
    assert store.excess() == {(D3, "TRAVIS", "IN-PERSON"): 1}


def test_a_cross_method_repeat_sets_the_method_flag(store: _Store) -> None:
    store.add(D1, _v(1), "IN-PERSON")
    store.add(D2, _v(1), "MAIL-IN")
    _run(store)
    row = store.repeat(_v(1))
    assert row is not None
    assert row["flags"] == FLAG_CROSS_DAY | FLAG_CROSS_METHOD
    assert [a["method"] for a in row["appearances"]] == ["IN-PERSON", "MAIL-IN"]
    assert store.excess() == {(D2, "TRAVIS", "MAIL-IN"): 1}


def test_three_appearances_keep_their_order_and_count_two_excess(store: _Store) -> None:
    store.add(D1, _v(1), county="A")
    store.add(D3, _v(1), county="B", row_hash=1)
    store.add(D3, _v(1), county="C", row_hash=2)
    _run(store)
    row = store.repeat(_v(1))
    assert row is not None
    assert row["n_counted"] == 3
    assert row["counted_days"] == [D1, D3]
    assert [(a["day"], a["county"]) for a in row["appearances"]] == [
        (D1, "A"),
        (D3, "B"),
        (D3, "C"),
    ]
    assert row["flags"] == FLAG_SAME_DAY | FLAG_CROSS_DAY
    assert store.excess() == {(D3, "B", "IN-PERSON"): 1, (D3, "C", "IN-PERSON"): 1}


def test_a_voter_with_only_a_removal_event_gets_a_row_with_no_counted_days(
    store: _Store,
) -> None:
    store.add(D1, _v(2))
    store.removal(D2, _v(1))
    result = _run(store)
    row = store.repeat(_v(1))
    assert row is not None
    assert (row["n_counted"], row["n_removed"], row["first_day"]) == (0, 1, None)
    assert (row["counted_days"], row["appearances"], row["removed_days"]) == ([], [], [D2])
    assert row["flags"] == FLAG_REMOVED
    assert result.removed_voters == 1


def test_a_live_voter_removed_from_another_day_keeps_its_single_appearance(
    store: _Store,
) -> None:
    store.add(D1, _v(1))
    store.removal(D3, _v(1), "n1")
    store.removal(D3, _v(1), "n2")
    store.removal(D2, _v(1), "n1")
    _run(store)
    row = store.repeat(_v(1))
    assert row is not None
    assert (row["n_counted"], row["n_removed"], row["first_day"]) == (1, 3, D1)
    assert row["removed_days"] == [D2, D3]
    assert row["appearances"] == [{"day": D1, "method": "IN-PERSON", "county": "TRAVIS"}]
    assert row["flags"] == FLAG_REMOVED


def test_a_repeated_and_removed_voter_carries_both_flags(store: _Store) -> None:
    store.add(D1, _v(1))
    store.add(D3, _v(1))
    store.removal(D2, _v(1))
    _run(store)
    row = store.repeat(_v(1))
    assert row is not None
    assert row["flags"] == FLAG_CROSS_DAY | FLAG_REMOVED
    assert (row["counted_days"], row["removed_days"]) == ([D1, D3], [D2])


def test_added_events_in_the_change_log_are_not_removals(store: _Store) -> None:
    store.add(D1, _v(1))
    store.conn.execute(
        "INSERT INTO early_vote_day_change VALUES (?, ?, 'o', 'n', 'added', ?, 'TRAVIS', "
        "'IN-PERSON', 't')",
        (ELECTION, D1, _v(1)),
    )
    _run(store)
    assert store.repeat(_v(1)) is None


def test_rows_removed_from_the_roster_are_ignored(store: _Store) -> None:
    store.add(D1, _v(1))
    store.add(D2, _v(1), removed_in=1)
    result = _run(store)
    assert store.repeat(_v(1)) is None
    assert (result.raw_rows, result.distinct_voters) == (1, 1)


def _random_roster(store: _Store, seed: int) -> None:
    rng = random.Random(seed)
    days = [(date(2026, 10, 14) + timedelta(days=k)).isoformat() for k in range(5)]
    for n in range(400):
        for _ in range(rng.choice((1, 1, 1, 2, 2, 3))):
            store.add(
                rng.choice(days),
                _v(n),
                rng.choice(("IN-PERSON", "MAIL-IN")),
                rng.choice(("TRAVIS", "HAYS", "BEXAR")),
                # Half the keys share one row-hash range, so key order within a read matters.
                row_hash=rng.choice((rng.randrange(-(2**63), 2**63), rng.randrange(2**40))),
                removed_in=rng.choice((None, None, None, None, 1)),
            )


def test_raw_minus_excess_is_the_distinct_first_appearances_of_every_slice(
    store: _Store,
) -> None:
    _random_roster(store, 7)
    result = _run(store)
    raw = {
        (d, c, m): n
        for d, c, m, n in store.rows(
            "SELECT v.ev_date, r.county, r.voting_method, COUNT(*) FROM early_vote_roster_row r "
            "JOIN early_vote_roster_version v ON r.day = julianday(v.ev_date) - 1721424.5 "
            "WHERE r.removed_in IS NULL GROUP BY 1, 2, 3"
        )
    }
    # The requirement as SQL: a voter's first appearance is its earliest day, then lowest key.
    firsts = {
        (d, c, m): n
        for d, c, m, n in store.rows(
            "SELECT ev_date, county, voting_method, COUNT(*) FROM (SELECT v.ev_date, r.county, "
            "r.voting_method, ROW_NUMBER() OVER (PARTITION BY r.id_voter "
            "ORDER BY r.day, r.row_hash, r.occ) AS k FROM early_vote_roster_row r "
            "JOIN early_vote_roster_version v ON r.day = julianday(v.ev_date) - 1721424.5 "
            "WHERE r.removed_in IS NULL) WHERE k = 1 GROUP BY 1, 2, 3"
        )
    }
    excess = store.excess()
    assert sum(excess.values()) > 0
    for key, n in raw.items():
        assert n - excess.get(key, 0) == firsts.get(key, 0), key
    ((distinct,),) = store.rows(
        "SELECT COUNT(DISTINCT id_voter) FROM early_vote_roster_row WHERE removed_in IS NULL"
    )
    assert sum(raw.values()) - sum(excess.values()) == distinct == result.distinct_voters
    ((repeated,),) = store.rows("SELECT COUNT(*) FROM early_vote_vuid_repeat WHERE n_counted > 1")
    assert repeated == result.repeat_voters
    meta = store.meta()
    assert int(meta["raw_rows"]) == sum(raw.values())
    assert int(meta["distinct_voters"]) == distinct
    assert int(meta["excess_rows"]) == sum(excess.values())


def test_meta_is_written_last_and_holds_the_counts(store: _Store) -> None:
    store.add(D1, _v(1))
    store.add(D2, _v(1))
    store.add(D2, _v(2))
    store.removal(D2, _v(3))
    _run(store)
    assert "early_vote_repeat_meta" in store.statements[-1][0]
    assert all("early_vote_repeat_meta" not in sql for sql, _ in store.statements[:-1])
    meta = store.meta()
    assert {k: meta[k] for k in meta if k != "signature"} == {
        "raw_rows": "3",
        "distinct_voters": "2",
        "repeat_voters": "1",
        "excess_rows": "1",
        "removed_voters": "1",
    }
    assert re.fullmatch(r"[0-9a-f]{64}", meta["signature"])
    assert "built_at" not in meta


def test_a_second_apply_sends_no_statement(store: _Store) -> None:
    store.add(D1, _v(1))
    store.add(D2, _v(1))
    _run(store)
    before = len(store.statements)
    result = _run(store)
    assert result.status == "unchanged"
    assert len(store.statements) == before


def test_an_unchanged_signature_skips_the_roster_read(store: _Store) -> None:
    store.add(D1, _v(1))
    store.add(D2, _v(1))
    _run(store)
    reads: list[str] = []
    original = store._query_rows

    def spy(database_id: str, sql: str, params: list[str]) -> list[dict[str, Any]]:
        reads.append(sql)
        return original(database_id, sql, params)

    store._query_rows = spy  # ty: ignore[invalid-assignment]  # reason: test spy on a fake store
    _run(store)
    assert reads
    assert not any("FROM early_vote_roster_row" in sql for sql in reads)


def test_a_new_change_log_entry_changes_the_signature(store: _Store) -> None:
    store.add(D1, _v(1))
    _run(store)
    signature = store.meta()["signature"]
    store.removal(D2, _v(1))
    result = _run(store)
    assert result.status == "wrote"
    assert store.meta()["signature"] != signature
    assert store.repeat(_v(1)) is not None


def _upserted(statements: Sequence[tuple[str, list[str]]]) -> list[str]:
    return [
        row[0]
        for sql, params in statements
        if sql.startswith("INSERT INTO early_vote_vuid_repeat ")
        for row in json.loads(params[0])
    ]


def test_a_changed_roster_day_updates_only_the_affected_voters(store: _Store) -> None:
    for n in range(1, 6):
        store.add(D1, _v(n))
        store.add(D3, _v(n))
    _run(store)
    start = len(store.statements)
    store.add(D2, _v(2), "MAIL-IN")
    store.add(D2, _v(9))
    store.bump(D2)
    result = _run(store)
    assert _upserted(store.statements[start:]) == [_v(2)]
    assert (result.rows_written, result.rows_deleted) == (1, 0)
    row = store.repeat(_v(2))
    assert row is not None
    assert row["counted_days"] == [D1, D2, D3]
    assert store.excess() == {
        (D3, "TRAVIS", "IN-PERSON"): 5,
        (D2, "TRAVIS", "MAIL-IN"): 1,
    }


def test_a_voter_that_stops_qualifying_is_deleted_and_its_excess_goes(store: _Store) -> None:
    store.add(D1, _v(1))
    store.add(D3, _v(1), county="HAYS")
    store.add(D1, _v(2))
    store.add(D3, _v(2))
    _run(store)
    store.conn.execute(
        "UPDATE early_vote_roster_row SET removed_in = 2 WHERE id_voter = ? AND county = 'HAYS'",
        (_v(1),),
    )
    store.bump(D3)
    result = _run(store)
    assert store.repeat(_v(1)) is None
    assert store.repeat(_v(2)) is not None
    assert store.excess() == {(D3, "TRAVIS", "IN-PERSON"): 1}
    assert result.rows_deleted == 1


def test_writes_are_bounded_batches(store: _Store) -> None:
    for n in range(25):
        store.add(D1, _v(n))
        store.add(D2, _v(n), county=f"C{n}")
    _run(store, batch_rows=4)
    payloads = [
        json.loads(p[0])
        for sql, p in store.statements
        if sql.startswith(
            ("INSERT INTO early_vote_vuid_repeat ", "INSERT INTO early_vote_repeat_excess ")
        )
    ]
    assert max(len(batch) for batch in payloads) == 4
    assert len(_upserted(store.statements)) == 25
    assert len(store.excess()) == 25


def test_a_dry_run_on_a_fresh_database_creates_and_writes_nothing_and_reports() -> None:
    fresh = _Store(repeat_tables=False)
    fresh.add(D1, _v(1))
    fresh.add(D3, _v(1))
    fresh.add(D1, _v(2))
    fresh.removal(D2, _v(3))
    result = _run(fresh, dry_run=True)
    assert (fresh.schema_calls, fresh.statements) == (0, [])
    assert fresh.rows("SELECT name FROM sqlite_master WHERE name LIKE '%repeat%'") == []
    assert result.status == "dry_run"
    assert (result.raw_rows, result.distinct_voters, result.repeat_voters) == (3, 2, 1)
    assert (result.excess_rows, result.removed_voters) == (1, 1)
    assert (result.rows_pending, result.rows_written) == (2, 0)


def test_a_dry_run_after_an_apply_writes_nothing_and_finds_nothing_pending(
    store: _Store,
) -> None:
    store.add(D1, _v(1))
    store.add(D3, _v(1))
    _run(store)
    calls, before = store.schema_calls, len(store.statements)
    result = _run(store, dry_run=True)
    assert (store.schema_calls, len(store.statements)) == (calls, before)
    assert (result.status, result.rows_pending, result.rows_stale) == ("dry_run", 0, 0)


def test_no_database_means_nothing_to_do() -> None:
    class _None(_Store):
        @override
        def _database_uuid_if_exists(self, election_id: str) -> str | None:
            return None

    assert _run(_None()) is None


def test_logs_and_errors_carry_no_vuids(store: _Store, caplog: pytest.LogCaptureFixture) -> None:
    store.add(D1, _v(1))
    store.add(D3, _v(1))
    store.removal(D2, _v(2))
    with caplog.at_level(logging.DEBUG):
        _run(store)
    assert "repeat_voters=1" in caplog.text
    assert not re.search(r"\d{10}", caplog.text)
    store.add(D1, "12345ABCDE")
    store.add(D2, "12345ABCDE")
    store.bump(D2)
    with pytest.raises(EarlyVoteSyncError) as excinfo:
        _run(store)
    assert "12345ABCDE" not in str(excinfo.value)
    assert "vuid" in str(excinfo.value)


_HASH_SCRIPT = textwrap.dedent(
    """
    import json, sys
    sys.path.insert(0, {root!r})
    from tests.unit.test_repeat_build import _Store, _random_roster, _run
    store = _Store()
    _random_roster(store, 3)
    _run(store)
    print(json.dumps(store.rows("SELECT vuid, src_hash FROM early_vote_vuid_repeat ORDER BY vuid")))
    print(store.meta()["signature"])
    """
)


def test_hashes_do_not_depend_on_pythonhashseed() -> None:
    root = str(Path(__file__).resolve().parents[2])
    outputs = []
    for seed in ("1", "2"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        script = _HASH_SCRIPT.format(root=root)
        out = subprocess.run(
            [sys.executable, "-c", script], check=True, capture_output=True, text=True, env=env
        )
        outputs.append(out.stdout)
    assert outputs[0] == outputs[1]
    assert len(json.loads(outputs[0].splitlines()[0])) > 10


def _normalize(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).replace("( ", "(").replace(" )", ")").strip()


@pytest.mark.parametrize(
    ("model", "constant"),
    [
        (VuidRepeatRow, REPEAT_TABLE_SQL),
        (RepeatExcessRow, EXCESS_TABLE_SQL),
        (RepeatMeta, META_TABLE_SQL),
    ],
)
def test_committed_repeat_ddl_matches_what_the_models_generate(model: Any, constant: str) -> None:
    generated = str(
        CreateTable(model.__table__, if_not_exists=True).compile(dialect=sqlite.dialect())
    )
    assert _normalize(constant) == _normalize(generated)


def test_repeat_columns_match_the_model() -> None:
    columns = VuidRepeatRow.__table__.columns.keys()  # ty: ignore[unresolved-attribute]  # reason: SQLModel sets it at class creation
    assert list(REPEAT_COLUMNS) == list(columns)


def test_the_sync_schema_creates_the_repeat_tables() -> None:
    for sql in REPEAT_SCHEMA_CREATES:
        assert sql in _SCHEMA_CREATES


_HEAVY = (
    "('sqlmodel', 'sqlalchemy', 'duckdb', 'shapely', 'pyproj', 'polars', 'geopandas', "
    "'voterfile_audit_pipeline')"
)


@pytest.mark.parametrize(
    "module",
    ["texas_turnout_scraper.early_vote_sync", "texas_turnout_scraper.repeat_tables"],
)
def test_worker_side_modules_import_no_heavy_libraries(module: str) -> None:
    code = (
        f"import sys, {module};bad = [m for m in {_HEAVY} if m in sys.modules];assert not bad, bad"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_the_worker_app_import_graph_loads_no_heavy_libraries() -> None:
    worker_src = Path(__file__).resolve().parents[2] / "infra" / "preview" / "worker" / "src"
    code = (
        f"import sys, ev_api.app; bad = [m for m in {_HEAVY} if m in sys.modules];"
        "assert not bad, bad"
    )
    env = {**os.environ, "PYTHONPATH": str(worker_src)}
    subprocess.run([sys.executable, "-c", code], check=True, env=env)


def test_the_build_imports_no_duckdb_or_geo_libraries() -> None:
    code = (
        "import sys, texas_turnout_scraper.repeat_build;"
        "bad = [m for m in ('duckdb', 'shapely', 'pyproj') if m in sys.modules];"
        "assert not bad, bad"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


# --- memory ---------------------------------------------------------------------------------


def _bulk(store: _Store, rows: int, repeated_share: float) -> int:
    """``rows`` live rows over 11 days; ``repeated_share`` of the voters appear twice."""
    days = [(date(2026, 10, 14) + timedelta(days=k)).isoformat() for k in range(11)]
    rng = random.Random(11)
    voters = round(rows / (1 + repeated_share))
    repeated = rows - voters
    batch = []
    for n in range(voters):
        listings = [rng.randrange(11)]
        if n < repeated:
            listings.append(10 if n % 2 else listings[0])
        for k in listings:
            batch.append(
                (
                    f"C{n % 254:03d}",
                    _v(n),
                    "MAIL-IN" if n % 17 == 0 else "IN-PERSON",
                    day_number(days[k]),
                    rng.randrange(-(2**63), 2**63),
                )
            )
    store.conn.executemany(
        "INSERT INTO early_vote_roster_row (county, voter_name, id_voter, voting_method, "
        "precinct, day, row_hash, occ, extra, added_in, removed_in) "
        "VALUES (?, 'FAKE PERSON', ?, ?, '1', ?, ?, 0, '{}', 1, NULL)",
        batch,
    )
    store.conn.executemany(
        "INSERT INTO early_vote_roster_version VALUES (?, ?, 1, 'f', 's', 0, 0)",
        [(ELECTION, d) for d in days],
    )
    return repeated


class _CountingStore(_Store):
    """Keeps only the size of each write, so the store itself adds nothing to the peak."""

    def __init__(self) -> None:
        super().__init__()
        self.writes: list[tuple[str, int]] = []

    @override
    def _statements(self, database_id: str, statements: list[tuple[str, list[str]]]) -> None:
        for sql, params in statements:
            self.writes.append((sql[:40], len(json.loads(params[0])) if params else 0))


def test_peak_memory_stays_under_600_bytes_per_roster_row_and_writes_are_bounded() -> None:
    import gc
    import time
    import tracemalloc

    rows = 150_000
    store = _CountingStore()
    repeated = _bulk(store, rows, 0.2)
    gc.collect()
    started = time.perf_counter()
    tracemalloc.start()
    try:
        result = _run(store)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    seconds = time.perf_counter() - started
    assert result.raw_rows == rows
    assert result.repeat_voters == repeated
    assert peak / rows < 600, f"peak {peak / rows:.0f} bytes per roster row in {seconds:.1f} s"
    sizes = [n for sql, n in store.writes if sql.startswith("INSERT INTO early_vote_vuid_rep")]
    assert sum(sizes) == repeated
    assert max(sizes) <= 2000
    logging.getLogger(__name__).warning(
        "repeat build 150k rows: %.2f s, %.0f bytes per row", seconds, peak / rows
    )


def test_removed_rows_counts_roster_removals_without_a_logged_event(store: _Store) -> None:
    store.add(D1, _v(1))
    store.add(D2, _v(2), removed_in=1)
    store.add(D2, _v(3), removed_in=1)
    store.removal(D2, _v(3))
    result = _run(store)
    assert result.removed_rows == 2
    assert result.removed_voters == 1


def test_validation_failures_are_counted_and_nothing_is_written(store: _Store) -> None:
    for n in (1, 2, 3):
        store.add(D1, f"{n}2345ABCDE")
        store.add(D2, f"{n}2345ABCDE")
    store.add(D1, _v(9))
    store.add(D2, _v(9))
    store.bump(D2)
    with pytest.raises(EarlyVoteSyncError) as excinfo:
        _run(store)
    message = str(excinfo.value)
    assert message.startswith("3 repeat rows failed validation; first: ")
    assert "2345ABCDE" not in message
    assert store.rows("SELECT COUNT(*) FROM early_vote_vuid_repeat") == [(0,)]
