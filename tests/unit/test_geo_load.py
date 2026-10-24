"""Geography in D1: an idempotent load of the committed JSON, and roster precinct aliases."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy.dialects import sqlite
from sqlalchemy.schema import CreateTable

from texas_turnout_scraper.early_vote_roster_rows import ROW_TABLE_SQL, VERSION_TABLE_SQL
from texas_turnout_scraper.early_vote_sync import _SCHEMA_CREATES
from texas_turnout_scraper.geo_load import (
    build_aliases,
    county_key,
    load_geo,
    resolve_precinct,
    run_geo_load_on_store,
)
from texas_turnout_scraper.geo_models import GeoArea, GeoMeta, GeoPrecinct, GeoPrecinctAlias
from texas_turnout_scraper.geo_tables import (
    ALIAS_TABLE_SQL,
    AREA_TABLE_SQL,
    GEO_SCHEMA_CREATES,
    META_TABLE_SQL,
    PRECINCT_TABLE_SQL,
)


class _SqliteStore:
    def __init__(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        for sql in (ROW_TABLE_SQL, VERSION_TABLE_SQL, *GEO_SCHEMA_CREATES):
            self.conn.execute(sql)
        self.statements: list[tuple[str, list[str]]] = []

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


def _precinct(
    fips: str, prec: str, cd: str | None = "1", share: float | None = 1.0
) -> dict[str, Any]:
    split = None if share is None else share < 0.99
    return {
        "pctkey": f"{int(fips)}{prec}",
        "county_fips": fips,
        "prec": prec,
        "cd": cd,
        "cd_share": share,
        "split_cd": split,
        "sd": "5",
        "sd_share": 1.0,
        "split_sd": False,
        "hd": "47",
        "hd_share": 0.75,
        "split_hd": True,
    }


def _geo(
    precincts: list[dict[str, Any]], extra_areas: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    areas = [
        {"kind": "county", "code": "453", "name": "Travis", "lat": 30.3, "lon": -97.7},
        {"kind": "county", "code": "123", "name": "DeWitt", "lat": 29.1, "lon": -97.3},
        {"kind": "county", "code": "283", "name": "La Salle", "lat": 28.3, "lon": -99.1},
        {"kind": "cd", "code": "1", "name": "Congressional District 1", "lat": 30.1, "lon": -97.1},
        *[
            {"kind": "precinct", "code": p["pctkey"], "name": p["prec"], "lat": 30.0, "lon": -97.0}
            for p in precincts
        ],
        *(extra_areas or []),
    ]
    return {"provenance": {"method": "test"}, "areas": areas, "precincts": precincts}


PRECINCTS = [
    _precinct("453", "0101"),
    _precinct("453", "0042"),
    _precinct("453", "004A"),
    _precinct("123", "0007", cd=None, share=None),
    _precinct("283", "0001"),
]


def _write(tmp_path: Path, data: dict[str, Any], name: str = "tx_geo.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(data, sort_keys=True))
    return path


@pytest.fixture
def store() -> _SqliteStore:
    return _SqliteStore()


def test_a_load_writes_areas_precincts_and_the_meta_row_last(
    store: _SqliteStore, tmp_path: Path
) -> None:
    path = _write(tmp_path, _geo(PRECINCTS))
    result = load_geo(store, "db", path)
    assert result.status == "wrote"
    assert (result.areas, result.precincts) == (len(PRECINCTS) + 4, len(PRECINCTS))
    assert store.rows("SELECT name, sha256 FROM geo_meta") == [
        ("tx_geo", hashlib.sha256(path.read_bytes()).hexdigest())
    ]
    assert "geo_meta" in store.statements[-1][0]
    assert all("geo_meta" not in sql for sql, _ in store.statements[:-1])
    assert store.rows(
        "SELECT pctkey, county_fips, prec, cd, cd_share, split_cd, hd_share, split_hd "
        "FROM geo_precinct WHERE pctkey = '1230007'"
    ) == [("1230007", "123", "0007", None, None, None, 0.75, 1)]
    assert store.rows("SELECT lat, lon FROM geo_area WHERE kind = 'county' AND code = '453'") == [
        (30.3, -97.7)
    ]


def test_a_second_load_of_the_same_file_writes_nothing(store: _SqliteStore, tmp_path: Path) -> None:
    path = _write(tmp_path, _geo(PRECINCTS))
    load_geo(store, "db", path)
    before = len(store.statements)
    assert load_geo(store, "db", path).status == "unchanged"
    assert len(store.statements) == before


def test_a_changed_file_updates_rows_and_removes_stale_ones(
    store: _SqliteStore, tmp_path: Path
) -> None:
    load_geo(store, "db", _write(tmp_path, _geo(PRECINCTS)))
    changed = [_precinct("453", "0101", cd="2"), *PRECINCTS[1:4]]
    path = _write(tmp_path, _geo(changed), "v2.json")
    result = load_geo(store, "db", path)
    assert (result.status, result.removed_precincts, result.removed_areas) == ("wrote", 1, 1)
    assert store.rows("SELECT cd FROM geo_precinct WHERE pctkey = '4530101'") == [("2",)]
    assert store.rows("SELECT pctkey FROM geo_precinct WHERE pctkey = '2830001'") == []
    assert store.rows("SELECT code FROM geo_area WHERE code = '2830001'") == []
    assert store.rows("SELECT sha256 FROM geo_meta") == [
        (hashlib.sha256(path.read_bytes()).hexdigest(),)
    ]


def test_a_crash_before_the_meta_row_is_redone_on_the_next_run(
    store: _SqliteStore, tmp_path: Path
) -> None:
    path = _write(tmp_path, _geo(PRECINCTS))
    real = store._statements

    def fail_on_meta(database_id: str, statements: list[tuple[str, list[str]]]) -> None:
        if any("geo_meta" in sql for sql, _ in statements):
            raise RuntimeError("crash")
        real(database_id, statements)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(store, "_statements", fail_on_meta)
        with pytest.raises(RuntimeError):
            load_geo(store, "db", path)
    assert load_geo(store, "db", path).status == "wrote"
    assert len(store.rows("SELECT * FROM geo_precinct")) == len(PRECINCTS)


def test_load_statements_are_bounded(store: _SqliteStore, tmp_path: Path) -> None:
    load_geo(store, "db", _write(tmp_path, _geo(PRECINCTS)), batch_rows=2)
    batched = [json.loads(p[-1]) for _, p in store.statements if "json_each" in _]
    assert batched
    assert max(len(batch) for batch in batched) == 2


def test_a_bad_row_in_the_file_stops_the_load_before_any_write(
    store: _SqliteStore, tmp_path: Path
) -> None:
    bad = _geo(PRECINCTS, [{"kind": "zip", "code": "1", "name": "x", "lat": 1.0, "lon": 1.0}])
    with pytest.raises(ValidationError):
        load_geo(store, "db", _write(tmp_path, bad))
    assert store.statements == []


@pytest.mark.parametrize(
    ("name", "key"),
    [("DE WITT", "DEWITT"), ("DeWitt", "DEWITT"), ("LA SALLE", "LASALLE"), ("O'BRIEN", "OBRIEN")],
)
def test_county_names_compare_by_letters_only(name: str, key: str) -> None:
    assert county_key(name) == key


KEYS = {
    ("453", "0101"): "4530101",
    ("453", "0042"): "4530042",
    ("453", "004A"): "453004A",
    ("123", "0007"): "1230007",
}


@pytest.mark.parametrize(
    ("fips", "precinct", "expected"),
    [
        ("453", "0101", ("4530101", "exact")),
        ("453", "101", ("4530101", "zfill4")),
        ("453", "42", ("4530042", "zfill4")),
        ("453", "4-A", ("453004A", "alnum_zfill4")),
        ("453", "4 2", ("4530042", "alnum_zfill4")),
        ("453", "9999", (None, "unmatched")),
        ("453", "", (None, "unmatched")),
        ("123", "0101", (None, "unmatched")),
        (None, "0101", (None, "unmatched")),
    ],
)
def test_precinct_rules_apply_in_order_within_one_county(
    fips: str | None, precinct: str, expected: tuple[str | None, str]
) -> None:
    assert resolve_precinct(fips, precinct, KEYS) == expected


def _roster(store: _SqliteStore, rows: list[tuple[str, str, int | None]]) -> None:
    store.conn.execute(
        "INSERT INTO early_vote_roster_version VALUES ('53815', '2026-10-20', 1, 'f', 's', 0, 0)"
    )
    day = 739909  # date(2026, 10, 20).toordinal()
    for index, (county, precinct, removed_in) in enumerate(rows):
        store.conn.execute(
            "INSERT INTO early_vote_roster_row (county, voter_name, id_voter, voting_method, "
            "precinct, day, row_hash, occ, extra, added_in, removed_in) "
            "VALUES (?, 'N', '0000000001', 'MAIL-IN', ?, ?, ?, 0, '{}', 1, ?)",
            (county, precinct, day, index * 7919 - 50_000, removed_in),
        )


ROSTER: list[tuple[str, str, int | None]] = [
    ("TRAVIS", "0101", None),
    ("TRAVIS", "0101", None),
    ("TRAVIS", "101", None),
    ("TRAVIS", "4-A", None),
    ("DE WITT", "7", None),
    ("DE WITT", "0101", None),
    ("NOWHERE", "0101", None),
    ("TRAVIS", "0042", 1),
]


def _aliases(store: _SqliteStore) -> list[tuple[Any, ...]]:
    return store.rows(
        "SELECT county, precinct, pctkey, rule FROM geo_precinct_alias ORDER BY county, precinct"
    )


def test_aliases_cover_every_live_pair_with_its_rule(store: _SqliteStore, tmp_path: Path) -> None:
    load_geo(store, "db", _write(tmp_path, _geo(PRECINCTS)))
    _roster(store, ROSTER)
    result = build_aliases(store, "db", "53815")
    assert _aliases(store) == [
        ("DE WITT", "0101", None, "unmatched"),
        ("DE WITT", "7", "1230007", "zfill4"),
        ("NOWHERE", "0101", None, "unmatched"),
        ("TRAVIS", "0101", "4530101", "exact"),
        ("TRAVIS", "101", "4530101", "zfill4"),
        ("TRAVIS", "4-A", "453004A", "alnum_zfill4"),
    ]
    assert (result.pairs, result.matched_pairs, result.unmatched_pairs) == (6, 4, 2)
    assert (result.rows, result.matched_rows) == (7, 5)


def test_a_second_alias_run_writes_nothing_and_vanished_pairs_are_deleted(
    store: _SqliteStore, tmp_path: Path
) -> None:
    load_geo(store, "db", _write(tmp_path, _geo(PRECINCTS)))
    _roster(store, ROSTER)
    build_aliases(store, "db", "53815")
    before = len(store.statements)
    assert build_aliases(store, "db", "53815").written == 0
    assert len(store.statements) == before
    store.conn.execute("UPDATE early_vote_roster_row SET removed_in = 2 WHERE county = 'NOWHERE'")
    result = build_aliases(store, "db", "53815")
    assert (result.written, result.removed) == (0, 1)
    assert ("NOWHERE", "0101", None, "unmatched") not in _aliases(store)


def test_a_dry_run_writes_nothing(store: _SqliteStore, tmp_path: Path) -> None:
    path = _write(tmp_path, _geo(PRECINCTS))
    assert load_geo(store, "db", path, dry_run=True).status == "dry_run"
    load_geo(store, "db", path)
    _roster(store, ROSTER)
    before = len(store.statements)
    assert build_aliases(store, "db", "53815", dry_run=True).pairs == 6
    assert len(store.statements) == before
    assert _aliases(store) == []


def test_alias_writes_are_bounded(store: _SqliteStore, tmp_path: Path) -> None:
    load_geo(store, "db", _write(tmp_path, _geo(PRECINCTS)))
    _roster(store, ROSTER)
    start = len(store.statements)
    build_aliases(store, "db", "53815", batch_rows=2)
    batches = [json.loads(p[-1]) for sql, p in store.statements[start:] if "json_each" in sql]
    assert sum(len(b) for b in batches) == 6
    assert max(len(b) for b in batches) == 2


def test_alias_logs_carry_counts_not_names_or_precincts(
    store: _SqliteStore, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    load_geo(store, "db", _write(tmp_path, _geo(PRECINCTS)))
    _roster(store, ROSTER)
    with caplog.at_level(logging.INFO):
        build_aliases(store, "db", "53815")
    text = caplog.text
    assert "matched_pairs=4" in text
    for value in ("TRAVIS", "DE WITT", "NOWHERE", "4-A", "0101"):
        assert value not in text


def _normalize(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).replace("( ", "(").replace(" )", ")").strip()


@pytest.mark.parametrize(
    ("model", "constant"),
    [
        (GeoArea, AREA_TABLE_SQL),
        (GeoPrecinct, PRECINCT_TABLE_SQL),
        (GeoPrecinctAlias, ALIAS_TABLE_SQL),
        (GeoMeta, META_TABLE_SQL),
    ],
)
def test_committed_geo_ddl_matches_what_the_models_generate(model: Any, constant: str) -> None:
    generated = str(
        CreateTable(model.__table__, if_not_exists=True).compile(dialect=sqlite.dialect())
    )
    assert _normalize(constant) == _normalize(generated)


def test_the_sync_schema_creates_the_geo_tables() -> None:
    for sql in GEO_SCHEMA_CREATES:
        assert sql in _SCHEMA_CREATES


def test_geo_libraries_are_not_core_dependencies() -> None:
    requirements = importlib.metadata.requires("texas-turnout-scraper") or []
    for name in ("shapely", "pyproj", "pyshp"):
        found = [r for r in requirements if r.split(";")[0].strip().startswith(name)]
        assert found, name
        assert all(re.search(r"extra == [\"']geo[\"']", r) for r in found), name
    sqlmodel = [r for r in requirements if r.split(";")[0].strip().startswith("sqlmodel")]
    assert all(re.search(r"extra == [\"']loader[\"']", r) for r in sqlmodel)


@pytest.mark.parametrize(
    "module",
    ["texas_turnout_scraper.early_vote_sync", "texas_turnout_scraper.geo_tables"],
)
def test_worker_side_modules_import_no_geo_libraries_and_no_sqlmodel(module: str) -> None:
    code = (
        f"import sys, {module};"
        "bad = [m for m in ('shapely', 'pyproj', 'shapefile', 'numpy', 'sqlmodel', 'sqlalchemy')"
        " if m in sys.modules];"
        "assert not bad, bad"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_the_loader_imports_no_geo_libraries() -> None:
    code = (
        "import sys, texas_turnout_scraper.geo_load;"
        "bad = [m for m in ('shapely', 'pyproj', 'shapefile') if m in sys.modules];"
        "assert not bad, bad"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


class _FreshStore(_SqliteStore):
    """A database that has the roster tables but none of the geography tables yet."""

    def __init__(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        for sql in (ROW_TABLE_SQL, VERSION_TABLE_SQL):
            self.conn.execute(sql)
        self.statements = []
        self.schema_calls = 0

    def _database_uuid_if_exists(self, election_id: str) -> str | None:
        return "db"

    def _ensure_schema(self, database_id: str) -> None:
        self.schema_calls += 1
        for sql in GEO_SCHEMA_CREATES:
            self.conn.execute(sql)


def _geo_tables(store: _SqliteStore) -> list[tuple[Any, ...]]:
    return store.rows("SELECT name FROM sqlite_master WHERE name LIKE 'geo_%'")


def test_a_dry_run_on_a_fresh_database_creates_nothing_and_still_reports_the_match(
    tmp_path: Path,
) -> None:
    path = _write(tmp_path, _geo(PRECINCTS))
    fresh = _FreshStore()
    _roster(fresh, ROSTER)
    loaded, aliases = run_geo_load_on_store(fresh, "53815", path, dry_run=True)
    assert fresh.schema_calls == 0
    assert fresh.statements == []
    assert _geo_tables(fresh) == []
    assert loaded.status == "dry_run"
    assert (loaded.areas, loaded.precincts) == (len(PRECINCTS) + 4, len(PRECINCTS))

    applied = _FreshStore()
    _roster(applied, ROSTER)
    _, expected = run_geo_load_on_store(applied, "53815", path, dry_run=False)
    assert applied.schema_calls == 1
    assert aliases is not None and expected is not None
    assert (
        aliases.pairs,
        aliases.matched_pairs,
        aliases.unmatched_pairs,
        aliases.rows,
        aliases.matched_rows,
    ) == (
        expected.pairs,
        expected.matched_pairs,
        expected.unmatched_pairs,
        expected.rows,
        expected.matched_rows,
    )
    assert (aliases.written, aliases.removed) == (0, 0)


def test_a_dry_run_on_a_loaded_database_runs_no_schema_step_and_writes_nothing(
    tmp_path: Path,
) -> None:
    path = _write(tmp_path, _geo(PRECINCTS))
    loaded_store = _FreshStore()
    _roster(loaded_store, ROSTER)
    run_geo_load_on_store(loaded_store, "53815", path, dry_run=False)
    schema_calls, statements = loaded_store.schema_calls, len(loaded_store.statements)
    run_geo_load_on_store(loaded_store, "53815", path, dry_run=True)
    assert loaded_store.schema_calls == schema_calls
    assert len(loaded_store.statements) == statements


def test_the_worker_app_import_graph_loads_no_geo_libraries_and_no_sqlmodel() -> None:
    worker_src = Path(__file__).resolve().parents[2] / "infra" / "preview" / "worker" / "src"
    code = (
        "import sys, ev_api.app;"
        "bad = [m for m in ('shapely', 'pyproj', 'shapefile', 'numpy', 'sqlmodel', 'sqlalchemy')"
        " if m in sys.modules];"
        "assert not bad, bad"
    )
    env = {**os.environ, "PYTHONPATH": str(worker_src)}
    subprocess.run([sys.executable, "-c", code], check=True, env=env)
