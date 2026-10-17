# Preview early-vote read API

FastAPI app aimed at a Cloudflare Python Worker. Preview only. This directory does not deploy, apply Terraform, or create Cloudflare resources.

**~60s KV propagation is not fit for election-night results.** A write can take up to 60 seconds to show up in other locations ([KV write guidance](https://developers.cloudflare.com/kv/api/write-key-value-pairs/)). `get()` without `cacheTtl` still uses the 60-second default edge cache ([KV read `cacheTtl`](https://developers.cloudflare.com/kv/api/read-key-value-pairs/#cachettl-parameter)). This API does not shorten that and does not add a faster path. It is for the daily early-vote file only.

## TTL

The read-client contract says old versions expire by TTL and does not set a number of seconds. `ttl_seconds` stays unset (`None`). The KV `put` omits `expirationTtl` until a caller passes one. This Worker does not choose a duration.

## Read path

Keys, the HTTP path, and the ETag come from `texas_turnout_scraper.civix.read_client` (`cache_key`, `http_path`, `etag_for_version`, `etag_matches`, `version_key`). They are not re-encoded here.

1. Require a Cloudflare Access service token (`CF-Access-Client-Id` and `CF-Access-Client-Secret`). No public read. Empty Worker secrets fail closed.
2. Read the contract version key. The election id stays a string.
3. The ETag is `etag_for_version` (for example `"v42"`). `If-None-Match` returns **304** and does not query D1.
4. Otherwise read `cache_key` (for example `ev:49664:v42:county:453`).
5. Query that election's D1 database only on a miss (`SELECT body FROM early_vote WHERE resource = ?`). The resource is the cache-key suffix. Old keys are not deleted.

`GET /elections/{source_election_id}/county/{county_id}` matches `http_path(election_id, "county", county_id)`.

The D1 writer and the schema migration are a different PR. This app only reads `body`.

## Worker entry

```python
from workers import asgi
Default = asgi.entrypoint(app)
```

Documented at [FastAPI on Python Workers](https://developers.cloudflare.com/workers/languages/python/packages/fastapi/). `compatibility_flags = ["python_workers"]`. Env arrives on `request.scope["env"]` (`workers-py` `request_to_scope`). KV binding `EV_KV`. D1 bindings come from the `EV_D1_BINDINGS` JSON map. Callers reserve the service binding name `EV_API` (`EV_API_BINDING` on the read client).

## Client

The swappable client is `texas_turnout_scraper.civix.read_client.EarlyVoteReadClient`. This directory does not ship a second one.
