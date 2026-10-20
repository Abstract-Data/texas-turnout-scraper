"""Consumer token admin against mocked Cloudflare HTTP."""

from __future__ import annotations

import json

import httpx
import respx

from texas_turnout_scraper.consumer_token_admin import ConsumerTokenAdmin
from texas_turnout_scraper.consumer_tokens import hash_token


@respx.mock
def test_mint_writes_d1_and_kv() -> None:
    account = "acct"
    d1_id = "db-uuid"
    kv_id = "kv-ns"
    digest_holder: list[str] = []

    def d1_route(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        batch = body["batch"]
        for item in batch:
            params = item.get("params") or []
            if item["sql"].startswith("INSERT INTO consumer_api_token"):
                digest_holder.append(params[1])
        return httpx.Response(200, json={"success": True, "result": [{"success": True}]})

    respx.post(url__regex=rf".*/accounts/{account}/d1/database/{d1_id}/query").mock(
        side_effect=d1_route
    )
    respx.put(url__regex=rf".*/accounts/{account}/storage/kv/namespaces/{kv_id}/values/.*").mock(
        return_value=httpx.Response(200, json={"success": True})
    )

    admin = ConsumerTokenAdmin(
        account_id=account,
        api_token="cf-token-not-logged",
        d1_database_id=d1_id,
        kv_namespace_id=kv_id,
    )
    token_id, plaintext = admin.mint("partner-a")
    admin.close()

    assert token_id
    assert plaintext
    assert digest_holder == [hash_token(plaintext)]
    assert respx.calls.call_count >= 2


@respx.mock
def test_revoke_deletes_kv_key() -> None:
    account = "acct"
    d1_id = "db-uuid"
    kv_id = "kv-ns"
    digest = hash_token("revoke-me")

    def d1_route(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sql = body["batch"][0]["sql"]
        if sql.startswith("SELECT token_hash"):
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "result": [{"results": [{"token_hash": digest}], "success": True}],
                },
            )
        return httpx.Response(200, json={"success": True, "result": [{"success": True}]})

    respx.post(url__regex=rf".*/accounts/{account}/d1/database/{d1_id}/query").mock(
        side_effect=d1_route
    )
    delete_route = respx.delete(
        url__regex=rf".*/accounts/{account}/storage/kv/namespaces/{kv_id}/values/.*"
    ).mock(return_value=httpx.Response(200, json={"success": True}))

    admin = ConsumerTokenAdmin(
        account_id=account,
        api_token="cf-token",
        d1_database_id=d1_id,
        kv_namespace_id=kv_id,
    )
    assert admin.revoke("tok-id-1") is True
    admin.close()
    assert delete_route.called
