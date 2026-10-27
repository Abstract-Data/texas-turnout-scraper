# Karl Rove report

Daily-refreshing, print-ready HTML and PDF summarizing VEP registrants by **VEP cohort year** and early votes in the 2026 General (`53815`), using **live APIs only** (fixture mode for CI).

## Regenerate locally

```bash
uv sync --dev --extra reports
playwright install chromium

# EXAMPLE output (no secrets)
uv run python -m reports.karl_rove build --fixture --output-dir dist/karl-rove

# Live APIs (keys via environment only — never commit)
export VEP_API_KEY=...
export VEP_BASE_URL=https://data.txvoterengagement.com   # optional
export EV_API_BASE_URL=https://ev-api-preview.abstractdata.io
export CF_ACCESS_CLIENT_ID=...
export CF_ACCESS_CLIENT_SECRET=...
export EV_API_TOKEN=...   # roster scope consumer token
export EV_API_AGGREGATES_TOKEN=...   # optional; defaults to EV_API_TOKEN
uv run python -m reports.karl_rove build --output-dir dist/karl-rove
```

GitHub Actions: workflow `karl-rove-report.yml` (daily, early morning Central) uploads `dist/karl-rove/` as a **Confidential** artifact with short retention. Generated HTML/PDF are **not** committed to the repo.

## Secret names (values in 1Password / GHA secrets only)

| Name | Purpose |
|------|---------|
| `VEP_API_KEY` | Bearer token for vep-match-fast (`counts` + `rows` scopes) |
| `VEP_BASE_URL` | Optional; defaults to `https://data.txvoterengagement.com` with fallback host |
| `EV_API_BASE_URL` or `EV_API_HOST` | Early-vote read API base URL |
| `CF_ACCESS_CLIENT_ID` | Cloudflare Access service token (edge) |
| `CF_ACCESS_CLIENT_SECRET` | Cloudflare Access service token (edge) |
| `EV_API_TOKEN` | Minted **roster**-scope consumer bearer token |
| `EV_API_AGGREGATES_TOKEN` | Optional **aggregates** token for `by-day` (else `EV_API_TOKEN`) |

## Data sources

### Section 1 — VEP registrations

`GET /v1/years` on vep-match-fast (`counts` scope). Response shape:

```http
HTTP/1.1 200 OK
Content-Type: application/json

{"years":[{"vep_year":2026,"matches":228265}],"total":228265}
```

### Section 2 — Early vote join

Early-vote **aggregates** (`aggregates` scope) supply the latest `ev_date` via `GET /elections/53815/aggregates/by-day`.

Distinct voters for that date use the **roster** route (no batch VUID lookup on the read API today):

```http
GET /elections/53815/roster/{ev_date}
Authorization: Bearer <roster-scope token>
```

VEP match rows stream from `GET /v1/matches.ndjson?year=<Y>&fields=core,voter&limit=10000` (`rows` scope). The `voter` field set includes names, zip, county, and prospect id; the report parser keeps **only** `vep_year`, `Voter_voter_id`, and `Voter_registration_date`, then drops the rest immediately. Join runs in memory; **only aggregate counts** appear in HTML/PDF and workflow artifacts. Confirm field names on a live run with `GET /v1/fields` when a key is available.

**Alternative (not implemented):** read-only Cloudflare D1 REST with ~7k-VUID batches (~34 calls) would need `CLOUDFLARE_API_TOKEN`, account id, and database id — slower and more secrets than one roster CSV stream. A dedicated Worker endpoint for VUID membership would be cleaner; see PR body if pursued later.

## PDF vs Data Studio

Google Data Studio (Looker Studio) PDF export is limited for custom print margins and running headers. This report uses **HTML + headless Chromium** (`playwright` `page.pdf`) for Letter-sized output with confidential header/footer.

## Hygiene

- Never log, print, store, or upload VUIDs or voter rows.
- Do not commit generated `dist/karl-rove/` artifacts from live runs.
