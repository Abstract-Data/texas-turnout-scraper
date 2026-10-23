"""HTTP surface: elections list, one day's roster, all days. Roster routes need a roster token."""

from __future__ import annotations

import sqlite3

import pytest
from ev_api.app import ReadDeps, create_app
from fastapi.testclient import TestClient

from tests.unit.test_early_vote_roster_stream import DAY1, DAY2, HEADER, _Db, _store_day
from texas_turnout_scraper import early_vote_sync
from texas_turnout_scraper.consumer_tokens import hash_token, kv_key_for_hash
from texas_turnout_scraper.early_vote_d1_precompute import PRECOMPUTE_SCHEMA_CREATES
from texas_turnout_scraper.early_vote_sync import _SCHEMA_CREATES, sha256_decoded

ACCESS = {"CF-Access-Client-Id": "id.access", "CF-Access-Client-Secret": "secret-not-real"}
ROSTER = {"Authorization": "Bearer roster-secret"}
AGGREGATES = {"Authorization": "Bearer agg-secret"}


class _Kv:
    def __init__(self) -> None:
        self.values = {
            kv_key_for_hash(hash_token("roster-secret")): "roster",
            kv_key_for_hash(hash_token("agg-secret")): "aggregates",
        }

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def put(self, key: str, value: str, *, expiration_ttl: int | None) -> None:
        self.values[key] = value

    async def delete(self, key: str) -> None:
        self.values.pop(key, None)


class _Rosters:
    def __init__(self, dbs: dict[str, _Db]) -> None:
        self._dbs = dbs

    def election_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._dbs))

    def database(self, election_id: str) -> _Db | None:
        return self._dbs.get(election_id)


@pytest.fixture
def stored(monkeypatch: pytest.MonkeyPatch) -> _Db:
    monkeypatch.setattr(early_vote_sync, "CHUNK_RAW_BYTES", 16)
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    for sql in (*_SCHEMA_CREATES, *PRECOMPUTE_SCHEMA_CREATES):
        conn.execute(sql)
    _store_day(conn, "2026-10-19", DAY1)
    _store_day(conn, "2026-10-20", DAY2)
    return _Db(conn)


@pytest.fixture
def client(stored: _Db) -> TestClient:
    deps = ReadDeps(
        kv=_Kv(),
        store=None,  # type: ignore[arg-type]
        access_client_id="id.access",
        access_client_secret="secret-not-real",
        rosters=_Rosters({"53815": stored}),
    )
    return TestClient(create_app(deps))


def test_roster_needs_a_roster_token(client: TestClient) -> None:
    url = "/elections/53815/roster/2026-10-19"
    assert client.get(url).status_code == 401
    assert client.get(url, headers=ACCESS).status_code == 401
    assert client.get(url, headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get(url, headers=AGGREGATES).status_code == 403
    assert client.get(url, headers=ROSTER).status_code == 200
    assert client.get("/elections/53815/roster", headers=AGGREGATES).status_code == 403
    assert client.get("/elections/53815/roster", headers=ACCESS).status_code == 401


def test_one_day_is_streamed_exactly_with_headers(client: TestClient) -> None:
    response = client.get("/elections/53815/roster/2026-10-19", headers=ROSTER)
    assert response.content == DAY1
    assert response.headers["content-type"].startswith("text/csv")
    assert response.headers["content-length"] == str(len(DAY1))
    assert response.headers["etag"] == f'"{sha256_decoded(DAY1)}"'
    assert "private" in response.headers["cache-control"]
    assert "no-store" not in response.headers["cache-control"]
    assert 'filename="roster_53815_2026-10-19.csv"' in response.headers["content-disposition"]


def test_a_matching_etag_returns_304_without_reading_chunks(
    client: TestClient, stored: _Db
) -> None:
    url = "/elections/53815/roster/2026-10-19"
    etag = client.get(url, headers=ROSTER).headers["etag"]
    before = stored.queries
    response = client.get(url, headers={**ROSTER, "If-None-Match": etag})
    assert response.status_code == 304
    assert response.content == b""
    assert stored.queries - before <= 1


def test_all_days_is_one_csv_with_a_date_column(client: TestClient) -> None:
    response = client.get("/elections/53815/roster", headers=ROSTER)
    assert response.status_code == 200
    lines = response.text.splitlines()
    assert lines[0] == "EV_DATE," + HEADER.strip()
    assert lines[1].startswith("2026-10-19,0000000001,")
    assert lines[-1].startswith("2026-10-20,0000000003,")
    assert len(lines) == 4
    assert "content-length" not in response.headers
    assert 'filename="roster_53815_all_days.csv"' in response.headers["content-disposition"]
    again = client.get(
        "/elections/53815/roster", headers={**ROSTER, "If-None-Match": response.headers["etag"]}
    )
    assert again.status_code == 304


def test_bad_inputs(client: TestClient) -> None:
    assert client.get("/elections/53815/roster/2026-13-99x", headers=ROSTER).status_code == 400
    assert client.get("/elections/abc/roster", headers=ROSTER).status_code == 400
    assert client.get("/elections/99999/roster", headers=ROSTER).status_code == 404
    assert client.get("/elections/53815/roster/2099-01-01", headers=ROSTER).status_code == 404


def test_elections_list_is_open_to_any_valid_credential_and_has_no_rows(client: TestClient) -> None:
    assert client.get("/elections").status_code == 401
    for headers in (ROSTER, AGGREGATES, ACCESS):
        assert client.get("/elections", headers=headers).status_code == 200
    body = client.get("/elections", headers=AGGREGATES).json()
    (election,) = body["elections"]
    assert election["source_election_id"] == "53815"
    assert [d["ev_date"] for d in election["days"]] == ["2026-10-19", "2026-10-20"]
    assert election["days"][0]["rows"] == 2
    assert election["days"][0]["sha256"] == sha256_decoded(DAY1)
    assert election["total_rows"] == 3
    text = client.get("/elections", headers=AGGREGATES).text
    assert "DOE" not in text
    assert "0000000001" not in text


def test_elections_search_and_detail(client: TestClient) -> None:
    assert client.get("/elections?q=538", headers=AGGREGATES).json()["elections"]
    assert client.get("/elections?q=999", headers=AGGREGATES).json()["elections"] == []
    detail = client.get("/elections/53815", headers=AGGREGATES)
    assert detail.status_code == 200
    assert detail.json()["source_election_id"] == "53815"
    assert client.get("/elections/99999", headers=AGGREGATES).status_code == 404
    assert client.get("/elections/abc", headers=AGGREGATES).status_code == 400
