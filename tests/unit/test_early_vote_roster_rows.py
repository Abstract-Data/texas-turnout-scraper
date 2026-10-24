"""Roster rows keyed by content within a day, with version ranges: only the diff is written."""

from __future__ import annotations

import csv
import json
import sqlite3
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from typing_extensions import override

from texas_turnout_scraper import early_vote_sync
from texas_turnout_scraper.early_vote_roster_rows import (
    ROW_SCHEMA_CREATES,
    VISIBLE_AT_SQL,
    VISIBLE_PAGE_SQL,
    RosterSchemaError,
    day_number,
    gc_statements,
    visible_params,
)
from texas_turnout_scraper.early_vote_sync import (
    _SCHEMA_CREATES,
    CloudflareEarlyVoteStore,
    build_publish_plan,
)
from texas_turnout_scraper.roster_models import (
    RosterReject,
    RosterRow,
    iter_row_keys,
    load_roster_day,
)

EV_DATE = "2026-10-19"
DAY = day_number(EV_DATE)
HEADER = "COUNTY,VOTER_NAME,ID_VOTER,VOTING_METHOD,PRECINCT,POLL_PLACE_NAME"
A = "TRAVIS,ANNA ALPHA,1,MAIL-IN,101,CITY HALL"
B = 'BEXAR,"BRAVO, BOB",2,IN-PERSON,102,'
C = "HARRIS,CARL CHARLIE,3,IN-PERSON,,LIBRARY"
D = "HARRIS,DORA DELTA,4,MAIL-IN,,LIBRARY"
BAD = "HARRIS,BAD ECHO,NOPE,IN-PERSON,,LIBRARY"
BAD2 = "DALLAS,WORSE FOXTROT,X1,EARLY,,"
NAMES = ("ANNA ALPHA", "BRAVO, BOB", "CARL CHARLIE", "DORA DELTA", "BAD ECHO", "WORSE FOXTROT")
VUIDS = ("0000000001", "0000000002", "0000000003", "0000000004")


def _csv(*lines: str, header: str = HEADER) -> bytes:
    return ("\n".join((header, *lines)) + "\n").encode()


class _Store:
    """The two store primitives the day loader uses, backed by a real sqlite database."""

    def __init__(self, creates: tuple[str, ...] = ROW_SCHEMA_CREATES) -> None:
        self.conn = sqlite3.connect(":memory:")
        for sql in creates:
            self.conn.execute(sql)
        self.sent: list[tuple[str, list[str]]] = []
        self.files: dict[str, bytes] = {}
        self.before: Callable[[str], None] | None = None

    def _query_rows(self, database_id: str, sql: str, params: list[str]) -> list[dict[str, Any]]:
        cur = self.conn.execute(sql, params)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]

    def _statements(self, database_id: str, statements: list[tuple[str, list[str]]]) -> None:
        for sql, params in statements:
            if self.before is not None:
                self.before(sql)
            self.sent.append((sql, list(params)))
            self.conn.execute(sql, params)

    def load(self, decoded: bytes, file_id: str, *, base_missing: bool = False):
        self.files[file_id] = decoded
        self.sent.clear()
        files = {} if base_missing else self.files
        return load_roster_day(
            self,
            "db",
            "53815",
            EV_DATE,
            file_id=file_id,
            sha256=f"sha-{file_id}",
            decoded=decoded,
            load_base=files.get,
        )

    def visible(self, version: int, table: str = "early_vote_roster_row") -> list[str]:
        column = (
            "voter_name"
            if table == "early_vote_roster_row"
            else "json_extract(raw, '$.VOTER_NAME')"
        )
        rows = self.conn.execute(
            f"SELECT {column} FROM {table} WHERE day = ? AND {VISIBLE_AT_SQL}",
            (DAY, *visible_params(version)),
        ).fetchall()
        return sorted(r[0] for r in rows)

    def physical(self, table: str = "early_vote_roster_row") -> int:
        return int(self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    def version(self) -> tuple[Any, ...] | None:
        return self.conn.execute(
            "SELECT version, file_id, sha256, row_count, reject_count "
            "FROM early_vote_roster_version WHERE source_election_id = '53815' AND ev_date = ?",
            (EV_DATE,),
        ).fetchone()


def _kind(sql: str) -> str:
    if sql.startswith("INSERT INTO early_vote_roster_version"):
        return "flip"
    if sql.startswith("INSERT INTO early_vote_roster_"):
        return "add"
    if sql.startswith("UPDATE") and "IN (SELECT" in sql:
        return "remove"
    if sql.startswith("UPDATE") and "added_in <=" in sql:
        return "retire"
    if sql.startswith("DELETE") and "removed_in <=" in sql:
        return "gc"
    if "added_in >" in sql or "SET removed_in = NULL" in sql:
        return "recover"
    return "other"


def _kinds(store: _Store) -> list[str]:
    return [_kind(sql) for sql, _ in store.sent]


def _flip_hook(store: _Store, seen: dict[str, Any], old: int) -> None:
    def before(sql: str) -> None:
        if _kind(sql) == "flip":
            seen["at_old"] = store.visible(old)
            seen["at_new"] = store.visible(old + 1)
            seen["rejects_at_old"] = store.visible(old, "early_vote_roster_reject")

    store.before = before


def _crash_on(kind: str) -> Callable[[str], None]:
    def before(sql: str) -> None:
        if _kind(sql) == kind:
            raise RuntimeError(f"crashed at {kind}")

    return before


def _names(*lines: str) -> list[str]:
    return sorted(next(csv.reader([line]))[1] for line in lines)


def test_first_load_writes_every_row_at_version_1_and_the_marker() -> None:
    store = _Store()
    result = store.load(_csv(A, B, C, BAD), "f1")
    assert (result.version, result.added, result.removed) == (1, 4, 0)
    assert store.visible(1) == _names(A, B, C)
    assert store.visible(1, "early_vote_roster_reject") == ["BAD ECHO"]
    assert store.version() == (1, "f1", "sha-f1", 3, 1)
    assert store.conn.execute(
        "SELECT DISTINCT added_in, removed_in FROM early_vote_roster_row"
    ).fetchall() == [(1, None)]
    got = store.conn.execute(
        "SELECT id_voter, extra, typeof(id_voter), typeof(row_hash) FROM early_vote_roster_row "
        "WHERE county = 'TRAVIS'"
    ).fetchone()
    assert got[0] == "0000000001"
    assert json.loads(got[1]) == {"POLL_PLACE_NAME": "CITY HALL"}
    assert got[2:] == ("text", "integer")


def test_a_changed_file_writes_only_the_added_and_removed_keys() -> None:
    store = _Store()
    store.load(_csv(A, B, C, BAD), "f1")
    result = store.load(_csv(A, B, D, BAD2), "f2")
    assert (result.version, result.added, result.removed) == (2, 2, 2)
    kinds = _kinds(store)
    assert kinds.count("add") == 2
    assert kinds.count("remove") == 2
    assert kinds.count("flip") == 1
    assert "retire" not in kinds
    unchanged = [str(h) for h, _ in iter_row_keys(_csv(A, B))]
    sent = " ".join(" ".join(params) for sql, params in store.sent if _kind(sql) != "flip")
    assert all(f"[{h}," not in sent for h in unchanged)
    assert store.visible(2) == _names(A, B, D)
    assert store.visible(2, "early_vote_roster_reject") == ["WORSE FOXTROT"]
    assert store.version() == (2, "f2", "sha-f2", 3, 1)


def test_an_unchanged_file_writes_no_row_statements_but_the_marker_follows_the_file() -> None:
    store = _Store()
    store.load(_csv(A, B, BAD), "f1")
    result = store.load(_csv(A, B, BAD), "f2")
    assert _kinds(store) == ["flip"]
    assert (result.version, result.added, result.removed) == (1, 0, 0)
    assert store.version() == (1, "f2", "sha-f2", 2, 1)


def test_a_reader_pinned_to_the_old_version_sees_the_old_set_until_the_flip() -> None:
    store = _Store()
    store.load(_csv(A, B, C, BAD), "f1")
    seen: dict[str, Any] = {}
    _flip_hook(store, seen, old=1)
    store.load(_csv(A, D, BAD2), "f2")
    assert seen["at_old"] == _names(A, B, C)
    assert seen["rejects_at_old"] == ["BAD ECHO"]
    assert seen["at_new"] == _names(A, D)
    assert store.visible(1) == _names(A)
    assert store.visible(2) == _names(A, D)


def test_marker_counts_equal_the_visible_counts_after_every_load() -> None:
    store = _Store()
    for file_id, decoded in (
        ("f1", _csv(A, B, BAD)),
        ("f2", _csv(A, C, D, BAD, BAD2)),
        ("f3", _csv(D)),
        ("f4", _csv(D, B)),
    ):
        store.load(decoded, file_id)
        state = store.version()
        assert state is not None
        version, _, _, rows, rejects = state
        assert rows == len(store.visible(version))
        assert rejects == len(store.visible(version, "early_vote_roster_reject"))


def test_writes_go_inserts_then_removals_then_the_flip_then_collection() -> None:
    store = _Store()
    store.load(_csv(A, B, C, BAD), "f1")
    store.load(_csv(A, D, BAD2), "f2")
    kinds = [k for k in _kinds(store) if k != "other"]
    last = {k: max(i for i, x in enumerate(kinds) if x == k) for k in set(kinds)}
    first = {k: min(i for i, x in enumerate(kinds) if x == k) for k in set(kinds)}
    assert last["recover"] < first["add"]
    assert last["add"] < first["remove"]
    assert last["remove"] < first["flip"]
    assert first["flip"] == last["flip"] < first["gc"]


def test_a_failed_attempt_is_cleaned_up_and_never_becomes_visible() -> None:
    store = _Store()
    store.load(_csv(A, B, C), "f1")
    store.before = _crash_on("flip")
    with pytest.raises(RuntimeError):
        store.load(_csv(A, D, BAD2), "failed")
    assert store.version() == (1, "f1", "sha-f1", 3, 0)
    assert store.visible(1) == _names(A, B, C)
    seen: dict[str, Any] = {}
    _flip_hook(store, seen, old=1)
    store.load(_csv(A, C), "f2")
    assert seen["at_old"] == _names(A, B, C)
    assert store.visible(2) == _names(A, C)
    assert store.visible(2, "early_vote_roster_reject") == []
    assert store.physical() == 2
    assert store.physical("early_vote_roster_reject") == 0


def test_a_failed_first_load_leaves_nothing_behind_on_the_next_one() -> None:
    store = _Store()
    store.before = _crash_on("flip")
    with pytest.raises(RuntimeError):
        store.load(_csv(A, B, C), "failed")
    store.before = None
    store.load(_csv(A), "f1")
    assert store.visible(1) == _names(A)
    assert store.physical() == 1


def test_identical_lines_get_distinct_occurrences_and_survive_a_diff() -> None:
    store = _Store()
    store.load(_csv(A, A, B), "f1")
    assert store.conn.execute(
        "SELECT occ FROM early_vote_roster_row WHERE county = 'TRAVIS' ORDER BY occ"
    ).fetchall() == [(0,), (1,)]
    store.load(_csv(A, B), "f2")
    assert store.visible(2) == _names(A, B)
    store.load(_csv(A, A, A, B), "f3")
    assert store.visible(3) == _names(A, A, A, B)


def test_a_header_change_replaces_every_key() -> None:
    store = _Store()
    store.load(_csv(A, B, BAD), "f1")
    renamed = HEADER.replace("POLL_PLACE_NAME", "POLL_SITE")
    result = store.load(_csv(A, B, BAD, header=renamed), "f2")
    assert (result.added, result.removed) == (3, 3)
    extras = store.conn.execute(
        f"SELECT extra FROM early_vote_roster_row WHERE day = ? AND {VISIBLE_AT_SQL}",
        (DAY, *visible_params(2)),
    ).fetchall()
    assert {next(iter(json.loads(e[0]))) for e in extras} == {"POLL_SITE"}


def test_a_row_that_comes_back_before_collection_is_revived_but_hidden_from_old_readers() -> None:
    store = _Store()
    store.load(_csv(A, B, C), "f1")
    store.before = _crash_on("gc")
    with pytest.raises(RuntimeError):
        store.load(_csv(A, B), "f2")
    assert (store.version() or ())[:1] == (2,)
    assert store.physical() == 3
    seen: dict[str, Any] = {}
    _flip_hook(store, seen, old=2)
    store.load(_csv(A, B, C), "f3")
    assert seen["at_old"] == _names(A, B)
    assert store.visible(3) == _names(A, B, C)
    assert store.physical() == 3


def test_collection_removes_only_rows_removed_at_or_before_the_new_version() -> None:
    store = _Store()
    for occ, removed in enumerate((None, 1, 2, 3)):
        store.conn.execute(
            "INSERT INTO early_vote_roster_row (day, row_hash, occ, county, voter_name, id_voter, "
            "voting_method, precinct, extra, added_in, removed_in) "
            "VALUES (?, 5, ?, 'C', 'N', '1', 'MAIL-IN', '', '{}', 1, ?)",
            (DAY, occ, removed),
        )
    for sql, params in gc_statements(DAY, 2, ranges=4):
        store.conn.execute(sql, params)
    left = store.conn.execute("SELECT removed_in FROM early_vote_roster_row ORDER BY occ")
    assert left.fetchall() == [(None,), (3,)]


def test_only_added_rows_are_validated(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"row": 0, "reject": 0}

    def spy(model: Any, key: str) -> Any:
        original = model.model_validate

        def counted(cls: Any, obj: Any, *args: Any, **kwargs: Any) -> Any:
            calls[key] += 1
            return original(obj, *args, **kwargs)

        return classmethod(counted)

    monkeypatch.setattr(RosterRow, "model_validate", spy(RosterRow, "row"))
    monkeypatch.setattr(RosterReject, "model_validate", spy(RosterReject, "reject"))
    store = _Store()
    store.load(_csv(A, B, C, BAD), "f1")
    assert calls == {"row": 4, "reject": 1}
    calls.update(row=0, reject=0)
    store.load(_csv(A, B, C, BAD, D, BAD2), "f2")
    assert calls == {"row": 2, "reject": 1}


def test_reject_reasons_carry_no_cell_values() -> None:
    store = _Store()
    store.load(_csv(BAD, BAD2, "TRAVIS,SHORT LINE"), "f1")
    reasons = [r[0] for r in store.conn.execute("SELECT reason FROM early_vote_roster_reject")]
    assert sorted(reasons) == [
        "column_count",
        "id_voter:value_error",
        "id_voter:value_error,voting_method:value_error",
    ]
    text = " ".join(reasons)
    for value in (*NAMES, "NOPE", "X1", "EARLY", "SHORT LINE"):
        assert value not in text


def test_a_missing_base_file_falls_back_to_a_full_replacement_old_readers_unaffected() -> None:
    store = _Store()
    store.load(_csv(A, B, C, BAD), "f1")
    seen: dict[str, Any] = {}
    _flip_hook(store, seen, old=1)
    result = store.load(_csv(A, D, BAD2), "f2", base_missing=True)
    assert "retire" in _kinds(store)
    assert result.version == 2
    assert seen["at_old"] == _names(A, B, C)
    assert seen["rejects_at_old"] == ["BAD ECHO"]
    assert store.visible(2) == _names(A, D)
    assert store.version() == (2, "f2", "sha-f2", 2, 1)
    assert store.physical() == 2
    assert store.physical("early_vote_roster_reject") == 1


def test_the_page_query_walks_a_version_in_key_order() -> None:
    store = _Store()
    store.load(_csv(A, B, C, D), "f1")
    store.load(_csv(A, B, D), "f2")
    keys = sorted(iter_row_keys(_csv(A, B, D)))
    after: tuple[int, int] = (-(2**63), -1)
    seen: list[tuple[int, int]] = []
    while True:
        page = store.conn.execute(
            VISIBLE_PAGE_SQL, (DAY, *visible_params(2), *map(str, after), "2")
        ).fetchall()
        if not page:
            break
        seen.extend((row[0], row[1]) for row in page)
        after = seen[-1]
    assert seen == keys


def test_logs_carry_counts_never_names_or_vuids(caplog: pytest.LogCaptureFixture) -> None:
    store = _Store()
    with caplog.at_level("DEBUG"):
        store.load(_csv(A, B, C, BAD), "f1")
        store.load(_csv(A, D, BAD2), "f2", base_missing=True)
        store.load(_csv(A, D, BAD2, B), "f3")
    for value in (*NAMES, *VUIDS, "NOPE", "X1"):
        assert value not in caplog.text


class _SqliteCloudflareStore(CloudflareEarlyVoteStore):
    """The real store, with its D1 calls answered by a sqlite database."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        super().__init__(
            account_id="a",
            api_token="t",
            kv_namespace_id="n",
            client=httpx.Client(),
            snapshots=None,
        )
        self.conn = conn
        self.sent: list[str] = []

    @override
    def _query_rows(self, database_id: str, sql: str, params: list[str]) -> list[dict[str, Any]]:
        cur = self.conn.execute(sql, params)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]

    @override
    def _statements(self, database_id: str, statements: list[tuple[str, list[str]]]) -> None:
        for sql, params in statements:
            self.conn.execute(sql, params)
            self.sent.append(sql)

    @override
    def stored_hash(self, election_id: str, *, ev_date_iso: str | None = None) -> str | None:
        return None


def _day_write(store: CloudflareEarlyVoteStore, decoded: bytes, file_id: str) -> None:
    plan = build_publish_plan("53815", decoded, f"sha-{file_id}", file_id)
    store._write_day_scoped("db", "53815", EV_DATE, plan, f"sha-{file_id}", decoded)


def test_a_day_write_moves_rows_by_diff_before_the_day_pointer_and_never_by_file_id() -> None:
    store = _schema_store()
    conn = store.conn
    _day_write(store, _csv(A, B, C, BAD), "f1")
    store.sent.clear()
    _day_write(store, _csv(A, B, D), "f2")
    sent = store.sent
    flip = next(
        i for i, s in enumerate(sent) if s.startswith("INSERT INTO early_vote_roster_version")
    )
    day = next(i for i, s in enumerate(sent) if s.startswith("INSERT INTO early_vote_day "))
    assert flip < day
    assert not any("file_id" in s for s in sent if "early_vote_roster_row" in s)
    assert conn.execute("SELECT DISTINCT file_id FROM early_vote_chunk").fetchall() == [("f2",)]
    names = conn.execute(
        f"SELECT voter_name FROM early_vote_roster_row WHERE day = ? AND {VISIBLE_AT_SQL}",
        (DAY, *visible_params(2)),
    ).fetchall()
    assert sorted(n[0] for n in names) == _names(A, B, D)


def _mock_store(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[CloudflareEarlyVoteStore, list[tuple[str, list[str]]]]:
    store = CloudflareEarlyVoteStore(
        account_id="a", api_token="t", kv_namespace_id="n", client=httpx.Client(), snapshots=None
    )
    statements: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(CloudflareEarlyVoteStore, "_query_rows", lambda self, db, sql, p: [])
    monkeypatch.setattr(
        CloudflareEarlyVoteStore, "_statements", lambda self, db, stmts: statements.extend(stmts)
    )
    monkeypatch.setattr(CloudflareEarlyVoteStore, "_insert_chunks", lambda self, db, ins: None)
    monkeypatch.setattr(
        CloudflareEarlyVoteStore, "_write_precomputed_aggregates", lambda self, *a, **k: None
    )
    return store, statements


def test_a_bad_header_stops_before_any_chunk_is_written(monkeypatch: pytest.MonkeyPatch) -> None:
    store, statements = _mock_store(monkeypatch)
    chunks: list[object] = []
    monkeypatch.setattr(
        CloudflareEarlyVoteStore, "_insert_chunks", lambda self, db, ins: chunks.append(ins)
    )
    with pytest.raises(RosterSchemaError):
        _day_write(store, b"COUNTY,VOTER_NAME,ID_VOTER\nTRAVIS,A B,1\n", "f1")
    assert chunks == []
    assert statements == []


def test_turnout_json_payloads_store_no_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    store, statements = _mock_store(monkeypatch)
    _day_write(store, b'{"turnout_by_county":[]}', "f1")
    assert not any("early_vote_roster" in s[0] for s in statements)


def _roster_log_levels(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.levelname for r in caplog.records if "roster rows election_id" in r.getMessage()]


def _schema_store() -> _SqliteCloudflareStore:
    conn = sqlite3.connect(":memory:")
    for sql in _SCHEMA_CREATES:
        conn.execute(sql)
    return _SqliteCloudflareStore(conn)


def test_a_day_with_rejects_is_logged_as_a_warning_with_counts(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("INFO"):
        _day_write(_schema_store(), _csv(A, B, BAD), "f1")
    assert _roster_log_levels(caplog) == ["WARNING"]
    assert "rows=2 rejects=1" in caplog.text
    for value in (*NAMES, *VUIDS):
        assert value not in caplog.text


def test_a_clean_day_is_logged_as_info(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("INFO"):
        _day_write(_schema_store(), _csv(A), "f1")
    assert _roster_log_levels(caplog) == ["INFO"]


LEGACY_DDL = (
    "CREATE TABLE early_vote_roster_row (county VARCHAR NOT NULL, voter_name VARCHAR NOT NULL, "
    "id_voter VARCHAR NOT NULL, voting_method VARCHAR NOT NULL, precinct VARCHAR NOT NULL, "
    "file_id VARCHAR NOT NULL, row_id INTEGER NOT NULL, extra JSON NOT NULL, "
    "PRIMARY KEY (file_id, row_id)) WITHOUT ROWID",
    "CREATE TABLE early_vote_roster_reject (file_id VARCHAR NOT NULL, row_id INTEGER NOT NULL, "
    "reason VARCHAR NOT NULL, raw JSON NOT NULL, PRIMARY KEY (file_id, row_id))",
    "CREATE TABLE early_vote_roster_rows (source_election_id VARCHAR NOT NULL, "
    "file_id VARCHAR NOT NULL, row_count INTEGER NOT NULL, reject_count INTEGER NOT NULL, "
    "PRIMARY KEY (source_election_id, file_id))",
)


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[0] for r in conn.execute(f"SELECT name FROM pragma_table_info('{table}')")}


def test_the_file_keyed_tables_are_dropped_and_replaced_once() -> None:
    conn = sqlite3.connect(":memory:")
    for sql in LEGACY_DDL:
        conn.execute(sql)
    conn.execute(
        "INSERT INTO early_vote_roster_row VALUES ('C', 'N', '1', 'MAIL-IN', '', 'f', 0, '{}')"
    )
    store = _SqliteCloudflareStore(conn)
    store._ensure_schema("db")
    assert "file_id" not in _columns(conn, "early_vote_roster_row")
    assert {"day", "row_hash", "occ", "added_in", "removed_in"} <= _columns(
        conn, "early_vote_roster_row"
    )
    assert "reason" in _columns(conn, "early_vote_roster_reject")
    assert "file_id" not in _columns(conn, "early_vote_roster_reject")
    assert _columns(conn, "early_vote_roster_rows") == set()
    assert "version" in _columns(conn, "early_vote_roster_version")
    assert _columns(conn, "early_vote_chunk")
    conn.execute(
        "INSERT INTO early_vote_roster_row (day, row_hash, occ, county, voter_name, id_voter, "
        "voting_method, precinct, extra, added_in) "
        "VALUES (1, 1, 0, 'C', 'N', '1', 'M', '', '{}', 1)"
    )
    store._ensure_schema("db")
    assert conn.execute("SELECT COUNT(*) FROM early_vote_roster_row").fetchone() == (1,)


def test_a_visible_count_that_disagrees_with_the_file_stops_before_the_version_flips() -> None:
    from texas_turnout_scraper.early_vote_errors import EarlyVoteSyncError

    store = _Store()
    store.load(_csv(A, B), "f1")
    store.conn.execute(
        "INSERT INTO early_vote_roster_row (day, row_hash, occ, county, voter_name, id_voter, "
        "voting_method, precinct, extra, added_in, removed_in) "
        "VALUES (?, 7, 0, 'X', 'STRAY ROW', '0000000009', 'MAIL-IN', '', '{}', 1, NULL)",
        (DAY,),
    )
    with pytest.raises(EarlyVoteSyncError) as excinfo:
        store.load(_csv(A, B, C), "f2")
    assert "STRAY" not in str(excinfo.value)
    assert store.version()[:2] == (1, "f1")


def _legacy_store(rows: int) -> _SqliteCloudflareStore:
    conn = sqlite3.connect(":memory:")
    for sql in LEGACY_DDL:
        conn.execute(sql)
    for index in range(rows):
        conn.execute(
            "INSERT INTO early_vote_roster_row VALUES ('C', 'N', '1', 'MAIL-IN', '', 'f', ?, '{}')",
            (index,),
        )
    return _SqliteCloudflareStore(conn)


def test_the_file_keyed_tables_are_emptied_in_bounded_batches_before_they_are_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(early_vote_sync, "LEGACY_DRAIN_BATCH", 3)
    store = _legacy_store(7)
    store._ensure_schema("db")
    drains = [s for s in store.sent if s.startswith("DELETE FROM early_vote_roster_row")]
    assert len(drains) == 3
    drops = [i for i, s in enumerate(store.sent) if s.startswith("DROP TABLE")]
    last_drain = max(i for i, s in enumerate(store.sent) if s in drains)
    assert drops and min(drops) > last_drain
    assert "file_id" not in _columns(store.conn, "early_vote_roster_row")


def test_an_interrupted_migration_resumes_where_it_stopped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(early_vote_sync, "LEGACY_DRAIN_BATCH", 3)
    store = _legacy_store(7)
    original = store._statements
    calls = {"n": 0}

    def flaky(database_id: str, statements: list[tuple[str, list[str]]]) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("d1 timeout")
        original(database_id, statements)

    monkeypatch.setattr(store, "_statements", flaky)
    with pytest.raises(RuntimeError):
        store._ensure_schema("db")
    left = store.conn.execute("SELECT COUNT(*) FROM early_vote_roster_row").fetchone()[0]
    assert 0 < left < 7
    monkeypatch.setattr(store, "_statements", original)
    store._ensure_schema("db")
    assert "file_id" not in _columns(store.conn, "early_vote_roster_row")
