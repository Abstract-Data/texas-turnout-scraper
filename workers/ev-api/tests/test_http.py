"""HTTP cache hit, cache miss, and 304 through the FastAPI app."""

from __future__ import annotations

import json
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from ev_api.app import ReadDeps, create_app  # noqa: E402
from ev_api.worker import Default  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from texas_turnout_scraper.civix.read_client import (  # noqa: E402
    cache_key,
    etag_for_version,
    http_path,
    version_key,
)

_ACCESS_ID = "preview-client.access"
_ACCESS_SECRET = "preview-secret-not-a-voter-id"
_AUTH = {
    "CF-Access-Client-Id": _ACCESS_ID,
    "CF-Access-Client-Secret": _ACCESS_SECRET,
}
_ELECTION = "049664"
_COUNTY = "0453"
_VERSION = 42


class MemoryKv:
    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values = dict(values or {})
        self.puts: list[tuple[str, str, int | None]] = []

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def put(self, key: str, value: str, *, expiration_ttl: int | None) -> None:
        self.puts.append((key, value, expiration_ttl))
        self.values[key] = value


class RecordingStore:
    def __init__(self, rows: dict[tuple[str, str], str] | None = None) -> None:
        self.rows = dict(rows or {})
        self.calls: list[tuple[str, str]] = []

    async def fetch_body(self, source_election_id: str, resource: str) -> str | None:
        self.calls.append((source_election_id, resource))
        return self.rows.get((source_election_id, resource))


def _client(kv: MemoryKv, store: RecordingStore) -> TestClient:
    app = create_app(
        ReadDeps(
            kv=kv,
            store=store,
            access_client_id=_ACCESS_ID,
            access_client_secret=_ACCESS_SECRET,
        )
    )
    return TestClient(app)


def _url() -> str:
    return http_path(_ELECTION, "county", _COUNTY)


def test_worker_entrypoint_is_unset_without_the_runtime() -> None:
    assert Default is None


def test_http_cache_hit() -> None:
    raw = json.dumps({"rows": 2, "ID_VOTER": "0000000001"})
    key = cache_key(_ELECTION, _VERSION, "county", _COUNTY)
    kv = MemoryKv({version_key(_ELECTION): str(_VERSION), key: raw})
    store = RecordingStore()
    response = _client(kv, store).get(_url(), headers=_AUTH)
    assert response.status_code == 200
    assert response.headers["etag"] == etag_for_version(_VERSION)
    assert response.json()["rows"] == 2
    assert response.json()["ID_VOTER"] == "0000000001"
    assert store.calls == []


def test_http_cache_miss_leaves_ttl_unset() -> None:
    raw = json.dumps({"rows": 4})
    kv = MemoryKv({version_key(_ELECTION): str(_VERSION)})
    store = RecordingStore({(_ELECTION, f"county:{_COUNTY}"): raw})
    response = _client(kv, store).get(_url(), headers=_AUTH)
    assert response.status_code == 200
    assert response.json() == {"rows": 4}
    assert store.calls == [(_ELECTION, f"county:{_COUNTY}")]
    assert kv.puts[0][0] == cache_key(_ELECTION, _VERSION, "county", _COUNTY)
    assert kv.puts[0][2] is None
    assert create_app().state.read_deps.ttl_seconds is None


def test_http_304() -> None:
    class BoomStore(RecordingStore):
        async def fetch_body(self, source_election_id: str, resource: str) -> str | None:
            raise AssertionError("d1 queried on 304")

    key = cache_key(_ELECTION, _VERSION, "county", _COUNTY)
    kv = MemoryKv({version_key(_ELECTION): str(_VERSION), key: '{"rows": 1}'})
    response = _client(kv, BoomStore()).get(
        _url(),
        headers={**_AUTH, "If-None-Match": etag_for_version(_VERSION)},
    )
    assert response.status_code == 304
    assert response.headers["etag"] == etag_for_version(_VERSION)
    assert response.content == b""


def test_http_requires_access_token() -> None:
    class BoomKv(MemoryKv):
        async def get(self, key: str) -> str | None:
            raise AssertionError(key)

    response = _client(BoomKv(), RecordingStore()).get(_url())
    assert response.status_code == 401
    assert response.content == b""
