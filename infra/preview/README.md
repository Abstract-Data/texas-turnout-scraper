# Preview Terraform

Preview/staging stack on the Abstract Data personal Cloudflare account. It creates one D1 shell database, one KV namespace, the Python Worker `tx-turnout-ev-api-preview`, a **custom domain** route, and a Cloudflare Access service token. There is no production workspace.

The Worker is reachable only on the configured custom domain (default `ev-api-preview.abstractdata.io` in zone `abstractdata.io` on the personal account). `workers.dev` and preview/version URLs stay off (`subdomain.enabled` and `subdomain.previews_enabled` are both false).

KV writes can take about 60 seconds to show up. That is not fit for election-night results. This module does not turn on KV instant mode.

Consumers bind to this Worker as `EV_API`. The read API is FastAPI on the deployed Python Worker bundle under `worker/`.

Preview Worker observability is enabled in Terraform with **`observability.logs.enabled = true`** and **`invocation_logs = false`**. Invocation logs capture inbound HTTP headers; this Worker accepts `CF-Access-Client-Secret`, `Authorization`, and `X-API-Key`, so invocation logging is off to avoid credential leakage in Workers Logs. Application **`console` / structured `logger` output** (for example unhandled exception type + route template only) remains available with **`head_sampling_rate = 1`** so operators still see aggregate read failures without sampling down debug signal on the low-traffic preview host. CI runs `tests/verify/check_ev_api_no_credential_logs.py` to block credential/header logging regressions in `ev_api`.

Python Workers need the `workers` SDK vendored into `worker/python_modules/` (via [Pywrangler](https://developers.cloudflare.com/workers/languages/python/packages/)). Terraform uploads `entry.py` plus that tree on `cloudflare_worker_version`. After changing `worker/pyproject.toml`, run:

```bash
cd infra/preview/worker
uv sync --frozen
uv run pywrangler sync --force
```

Commit `python_modules/` and `pylock.toml` with the change, including PyEmscripten `*.cpython-*-wasm32-emscripten.so` files (root `.gitignore` un-ignores them under `worker/python_modules/`). CI re-runs `pywrangler sync`, runs `scripts/verify_pyodide_startup.sh` (Pyodide import smoke), and fails if importable package trees or `pylock.toml` drift (wheel `.dist-info/` metadata is ignored — RECORD hashes and path-dep `direct_url.json` / `uv_cache.json` vary by machine).

Root `pyproject.toml` lists `infra/preview/worker/python_modules` in Ruff `extend-exclude` so `ruff check .` does not lint vendored upstream SDK (do not hand-edit that tree).

Pull requests run `terraform fmt`, `terraform validate`, and `tflint` only. They do not read secrets and they do not plan or apply.

A push to `main` or `workflow_dispatch` on `main` runs two jobs in the GitHub Environment `preview`. Plan is John's first approval. Apply is the second. John sees the plan before anything applies. Do not run `terraform plan` or `terraform apply` on a laptop.

## Early-vote cron trigger

`cloudflare_workers_cron_trigger.ev_sync` runs the Worker's `scheduled` handler on `0 15,16,22,23,3,4 * * *` (UTC). At 10:00, 17:00 and 22:00 Central until 11:59 PM on 2026-11-03 the handler dispatches `early-vote-sync.yml` on `main` with `trigger=cron`; at every other firing it does nothing. It does not scrape and does not write D1 or KV. See `docs/adr/013-worker-cron-dispatches-sync.md` and the runbook section "Early-vote schedule (Worker cron)".

The dispatch token comes from the `preview` environment secret `EV_DISPATCH_GITHUB_TOKEN` (a fine-grained token with only Actions: write on this repository). The plan step passes it as `TF_VAR_github_dispatch_token`; the variable is `sensitive` and defaults to empty, and the `GITHUB_DISPATCH_TOKEN` `secret_text` binding is added only when it is non-empty. The saved plan carries the value, so the apply step does not need it. The token also lands in the Terraform state in the same private bucket and in the Worker binding; rotate it by changing the secret and running the Terraform workflow.

## Workflow ingest spike (temporary)

`spike_ingest.tf` is the second spike for the early-vote Workflow ingest (`docs/adr/014-cloudflare-workflow-ingest.md`). It is temporary and everything in it is named `spike`.

| Resource | Name | Why |
| --- | --- | --- |
| `cloudflare_r2_bucket.spike_staging` | `tx-turnout-ev-staging-spike` | Private staging for the canonical day object and the per-range segments. No `cloudflare_r2_managed_domain` and no `cloudflare_r2_custom_domain` are declared, which are the only two ways a bucket is served publicly. The objects hold voter names, so it must stay private. |
| `cloudflare_r2_bucket_lifecycle.spike_staging` | rule `spike-expire-1-day` | Owner decision 4. Deletes objects and aborts incomplete multipart uploads after `max_age = 86400` seconds. R2's own default for abandoned multipart uploads is 7 days, which is too long for voter data. |
| `cloudflare_d1_database.spike_scratch` | `tx-turnout-ev-scratch` | Owner decision 7. Never added to `EV_D1_BINDINGS`, so the read API cannot reach it. |
| `cloudflare_workflow.spike_ingest` | `tx-turnout-ev-spike`, class `SpikeWorkflow` | The Hello-world Workflow. `default_retention` is 1 day for success and error, so step outputs do not sit for the account default of 3 or 30 days. |

The Worker version gains three bindings from `local.spike_bindings`: R2 `STAGING`, D1 `SCRATCH`, and Workflow `SPIKE_WORKFLOW`. `main.tf` carries two lines of the spike: `python_workflows` in `compatibility_flags`, and `local.spike_bindings` in the version's `bindings` concat. Python Workflows need both `python_workers` and `python_workflows` plus `compatibility_date` 2025-08-01 or later; the Worker is on 2026-10-17.

`class_name` must equal the module-level `SpikeWorkflow` name in `worker/entry.py`. The class is built by `install_workflow()`, which returns `None` when the `workers` package is absent, so unit tests import the module without the runtime.

### Post-apply checks

Both routes need Access headers to pass the edge and a minted consumer token to pass the Worker, the same as every other route. Export `EV_API_HOST`, `CF_ACCESS_CLIENT_ID`, `CF_ACCESS_CLIENT_SECRET` and `EV_API_TOKEN` first; do not log any of them.

```bash
# 1. Start a 300-step run with one deliberate failure at step 7.
curl -sS -X POST "https://$EV_API_HOST/spike/workflow/start?suffix=r1&steps=300&fail_at=7" \
  -H "CF-Access-Client-Id: $CF_ACCESS_CLIENT_ID" \
  -H "CF-Access-Client-Secret: $CF_ACCESS_CLIENT_SECRET" \
  -H "Authorization: Bearer $EV_API_TOKEN"

# 2. Start again with the same suffix. This is the duplicate-id question (R6).
curl -sS -X POST "https://$EV_API_HOST/spike/workflow/start?suffix=r1&steps=300&fail_at=7" \
  -H "CF-Access-Client-Id: $CF_ACCESS_CLIENT_ID" \
  -H "CF-Access-Client-Secret: $CF_ACCESS_CLIENT_SECRET" \
  -H "Authorization: Bearer $EV_API_TOKEN"

# 3. Poll the status until it is not "running".
curl -sS "https://$EV_API_HOST/spike/workflow/status?id=spike-r1" \
  -H "CF-Access-Client-Id: $CF_ACCESS_CLIENT_ID" \
  -H "CF-Access-Client-Secret: $CF_ACCESS_CLIENT_SECRET" \
  -H "Authorization: Bearer $EV_API_TOKEN"

# 4. Ask for more steps than the cap, to confirm the guard.
curl -sS -X POST "https://$EV_API_HOST/spike/workflow/start?suffix=r2&steps=9999" \
  -H "CF-Access-Client-Id: $CF_ACCESS_CLIENT_ID" \
  -H "CF-Access-Client-Secret: $CF_ACCESS_CLIENT_SECRET" \
  -H "Authorization: Bearer $EV_API_TOKEN"
```

| Response field | What it proves |
| --- | --- |
| `created: true` with an `instance_id` | The Terraform-declared `SPIKE_WORKFLOW` binding reaches a real Workflow and `create(id=, params=)` works from Python (R5). |
| `created: false`, `duplicate: true`, `create_error.class` on call 2 | Workflows rejects a duplicate instance id, and `get` finds the existing one. This is what makes the cron's `ev-{election}-{yyyymmddhh}` id safe (R6). |
| `status.output_counts.steps_run == 300` | A 300-step loop runs to the end inside one instance (R6, R12). |
| `status.output_counts.steps_ok == 299` and `steps_caught == 1` | A failed step is catchable and the run continues, which is what lets one failed day not kill the run (R6). |
| `status.converted: true` | A JS status object converts to Python through `to_py`. `converted: false` with a `raw_type` means it does not, and the real Workflow needs another reader. |
| `status.output_counts` present at all | A step output under 1 MiB round-trips and is readable from the Worker (R12, R13). |
| `steps: 400` on call 4 | The cap holds, so a caller cannot ask for an unbounded run. |
| `ctx_probe.ok: true` in the Workflow's own output | `ctx` injection into a `ctx` parameter works. `ok: false` with an `error_class` means the real Workflow cannot read `attempt` from the context. |

CPU per step is not in the response. Workers clocks do not advance during CPU, so read CPU milliseconds from Workers Logs for the Worker, not from any timing the route returns.

To remove the spike: delete `spike_ingest.tf`, `worker/src/ev_api/spike_workflow.py`, `tests/unit/test_ev_spike_workflow.py`, this section, the two spike lines in `main.tf`, the two mount lines in `worker/src/ev_api/app.py`, and the import plus `SpikeWorkflow` export in `worker/entry.py`.

## Routing and Access

Hostname choice: **Workers custom domain** on `abstractdata.io` (personal Cloudflare account), not `workers.dev`. Custom domain keeps preview off the shared `workers.dev` surface, matches production-like hostnames for Quick Results, and pairs with hostname-based Access. Cloudflare documents protecting Workers on custom domains via Access ([Cloudflare Access for Workers](https://developers.cloudflare.com/workers/configuration/cloudflare-access/), [custom domains](https://developers.cloudflare.com/workers/configuration/routing/custom-domains/#require-sign-in-for-a-custom-domain)).

Terraform attaches `cloudflare_workers_custom_domain` and keeps the existing Access application (`preview_service` / service token policy). The Worker destination plus a public hostname destination enforce Access on the custom domain.

### Request outcomes (preview only)

| Caller | Edge (Cloudflare Access) | Worker (`/health`, aggregate routes) |
| --- | --- | --- |
| No Access credentials | **401** or **403**, an Access block page (no public read) | Not reached |
| Access service token headers only (`CF-Access-Client-Id` / `CF-Access-Client-Secret`) | Allow | **401** (observed 2026-10-23) |
| Access service token headers + minted consumer bearer or `X-API-Key` | Allow | **200** on `/health` and data routes |
| Minted consumer bearer, no Access headers | **401**, blocked at the edge | Not reached |
| Invalid election id (for example `00000`) with valid credentials | Allow | **400** `bad_request` |

Every caller sends the Access service token headers to get past the edge and a minted consumer token to get past the Worker. Verified on the preview route on 2026-10-23 with a short-lived minted token: `/health` and all five aggregate routes returned 200, and `If-None-Match` returned 304.

The Worker's own service-token comparison (`ACCESS_CLIENT_ID` / `ACCESS_CLIENT_SECRET` bindings) did not accept the correct, current service token even though Access did. The likely cause is that Access does not forward the `CF-Access-Client-*` headers to the Worker, but that is unconfirmed. Do not rely on service-token headers alone. Do not log token values.

## Bootstrap

Do the GitHub settings before adding secrets. Do not try to apply from this pull request.

1. In the Cloudflare dashboard, confirm the account is the Abstract Data personal account, not the Quorum Report account.
2. Create a private R2 bucket before the first apply. Terraform cannot create the bucket it stores state in. Suggested name: `tx-turnout-tfstate-preview`. Leave public access off. Dashboard: R2 → Create bucket. Equivalent command, already authenticated to that account: `npx wrangler r2 bucket create tx-turnout-tfstate-preview`.
3. Create an R2 API token with Object Read & Write on that bucket only. Store the Access Key ID and Secret Access Key. These are S3 credentials, separate from `CLOUDFLARE_API_TOKEN`. See [R2 API tokens](https://developers.cloudflare.com/r2/api/tokens/). The S3 endpoint is `https://<ACCOUNT_ID>.r2.cloudflarestorage.com`. The region is `auto`. See [R2 S3 API](https://developers.cloudflare.com/r2/api/s3/api/).
4. In GitHub, open Settings → Environments → `preview` for `Abstract-Data/texas-turnout-scraper`. This is a settings step, not something this repository configures.
   - Deployment branches: selected branch `main` only.
   - Required reviewers: at least one.
5. Add these values as Environment secrets on `preview`. Do not add them as repository secrets. `CLOUDFLARE_API_TOKEN`, `R2_ACCESS_KEY_ID`, and `R2_SECRET_ACCESS_KEY` are not GitHub secrets.

| `preview` environment secret | Value |
| --- | --- |
| `CLOUDFLARE_ACCOUNT_ID` | Abstract Data personal account id, 32 characters. |
| `TF_STATE_BUCKET` | State bucket name, such as `tx-turnout-tfstate-preview`. |
| `TF_STATE_ENDPOINT` | `https://<ACCOUNT_ID>.r2.cloudflarestorage.com` |
| `OP_SERVICE_ACCOUNT_TOKEN` | 1Password service account token that can read Environment `s6tljdcafkgqxfuxhcnj3m5x5a`. |
| `EV_DISPATCH_GITHUB_TOKEN` | Optional. Fine-grained GitHub token, this repository only, Actions: write only. The Worker cron uses it to dispatch the early-vote sync. |

6. In that 1Password Environment, name these variables exactly:

| 1Password variable | Value |
| --- | --- |
| `CLOUDFLARE_API_TOKEN` | API token. Scopes below. |
| `R2_ACCESS_KEY_ID` | R2 S3 access key id for the state bucket. |
| `R2_SECRET_ACCESS_KEY` | R2 S3 secret access key for the state bucket. |
| `CLOUDFLARE_KV_NAMESPACE_ID` | Add after the first apply. Copy the `kv_namespace_id` output. |

`CLOUDFLARE_API_TOKEN` account scopes, limited to the Abstract Data personal account. No Quorum Report account. No Workers R2 Storage permission (state uses the R2 S3 keys). For the custom domain route, the token also needs read access to the `abstractdata.io` zone (or whichever `preview_zone_name` you use):

- Account → D1 → Edit
- Account → Workers KV Storage → Edit
- Account → Workers Scripts → Edit
- Account → Access: Apps and Policies → Write
- Account → Access: Service Tokens → Write
- Zone → DNS → Edit (zone `abstractdata.io`, personal account)
- Zone → Zone → Read (same zone)
- Zone → Workers Routes → Write (same zone)

Permission names: [Cloudflare API token permissions](https://developers.cloudflare.com/fundamentals/api/reference/permissions/).

Plan and apply load the 1Password variables with `op run --environment`. See [Read environment variables with op run](https://www.1password.dev/environments/read-environment-variables). `load-secrets-action` cannot load an Environment by id with a service account token. That whole-environment load is Workload Identity only (`OP_ENVIRONMENT_ID`), and the action README marks it as public preview. See [load-secrets-action](https://github.com/1Password/load-secrets-action/blob/main/README.md).

After those settings and secrets exist, a push to `main` waits for two `preview` approvals. The plan job is the first. It runs `terraform init` and `terraform plan -out=tfplan`, writes `terraform show -no-color tfplan` to the job summary, and stores the saved plan in the private state bucket at `plans/<run_id>-<attempt>/tfplan` plus `plans/<run_id>-<attempt>/tfplan.sha256`. The repository is public, so the plan file is not a GitHub artifact. The apply job is the second approval. It downloads that object, checks the SHA-256 against the plan job output, and runs `terraform apply -input=false tfplan`. It does not create a new plan. After a successful apply it deletes both objects. Do not point this workflow at a production environment.

After the first successful apply, the Access client id and secret are in the R2 state file. The dashboard does not show the secret again. On a trusted machine, with `OP_SERVICE_ACCOUNT_TOKEN` set and `preview.auto.tfvars` holding `account_id`, read them through the same `op run` wrapper. Do not print the token or the R2 keys.

```bash
cd infra/preview
op run --environment s6tljdcafkgqxfuxhcnj3m5x5a -- bash -c 'AWS_ACCESS_KEY_ID="$R2_ACCESS_KEY_ID" AWS_SECRET_ACCESS_KEY="$R2_SECRET_ACCESS_KEY" terraform output -raw access_client_id'
op run --environment s6tljdcafkgqxfuxhcnj3m5x5a -- bash -c 'AWS_ACCESS_KEY_ID="$R2_ACCESS_KEY_ID" AWS_SECRET_ACCESS_KEY="$R2_SECRET_ACCESS_KEY" terraform output -raw access_client_secret'
op run --environment s6tljdcafkgqxfuxhcnj3m5x5a -- bash -c 'AWS_ACCESS_KEY_ID="$R2_ACCESS_KEY_ID" AWS_SECRET_ACCESS_KEY="$R2_SECRET_ACCESS_KEY" terraform output -raw preview_api_url'
```

`backend.hcl` is gitignored and contains only:

```hcl
bucket    = "<TF_STATE_BUCKET>"
endpoints = { s3 = "<TF_STATE_ENDPOINT>" }
```

Send the id as `CF-Access-Client-Id` and the secret as `CF-Access-Client-Secret`. Do not log either value.

The preview Worker version binds `ACCESS_CLIENT_ID`, `ACCESS_CLIENT_SECRET` (from the Access service token above), shell D1 `DB` (consumer tokens), an imported election D1 (default `EV_53815` → `ev-53815`), and `EV_D1_BINDINGS` (JSON map from `preview_source_election_id` to that election binding name). Rotating the service token requires `terraform apply` so the Worker bindings pick up the new values.

## Rotating the Access service token

Rotate through Terraform, not the dashboard. The Worker's `ACCESS_CLIENT_SECRET` binding comes from `cloudflare_zero_trust_access_service_token.preview.client_secret` in state, and Cloudflare never returns that secret again. A dashboard rotation updates Access but leaves state and the Worker binding on the old value, so Access passes the request and the Worker answers 401.

To rotate: raise `client_secret_version` in `main.tf` above the version Cloudflare reports for the token (a dashboard rotation also raises it), and set `previous_client_secret_expires_at` to when the old secret should stop working. The provider then rotates, reads the new secret into state, and the next Worker version binds it. After apply, read the new secret with the `terraform output -raw access_client_secret` command above and store it in 1Password as `ACCESS_CLIENT_SECRET`.

## Consumer bearer tokens (minted)

Internal callers keep using Cloudflare Access service token headers. External consumers use minted tokens in addition to Access:

- `Authorization: Bearer <token>` or `X-API-Key: <token>` on aggregate routes and `/health`.
- Only HMAC-SHA256 digests (`v2:<hex>`, never plaintext) are stored. KV key prefix `consumer_token:<digest>` mirrors active rows in preview shell D1 table `consumer_api_token` (created on first `tx-turnout api generate`). Re-mint after digest scheme changes; legacy scrypt rows fail closed (401).

John manages tokens locally (1Password Environment `s6tljdcafkgqxfuxhcnj3m5x5a` plus Terraform outputs):

| Variable | Source |
| --- | --- |
| `CLOUDFLARE_ACCOUNT_ID` | Same as Terraform |
| `CLOUDFLARE_API_TOKEN` | Same scopes as early-vote sync |
| `CLOUDFLARE_PREVIEW_D1_DATABASE_ID` | `terraform output -raw d1_database_id` |
| `CLOUDFLARE_KV_NAMESPACE_ID` | `terraform output -raw kv_namespace_id` |

```bash
tx-turnout api generate --name "Partner Name" --email partner@example.org   # key stored in 1Password
tx-turnout api extend <token_id> --days 30
tx-turnout api list
tx-turnout api revoke <token_id>
```

The old `tx-turnout tokens ...` group is gone. See [Issuing API keys](#issuing-api-keys-expiring-stored-in-1password).

No Terraform apply is required for the token table; DDL is idempotent via the CLI. Do not commit or log plaintext tokens.

## Aggregate semantics

The portal publishes one file per early-vote date, and each file holds only that day's voters. The aggregate routes say which view they return in a `semantics` field.

| Route | `semantics` | Meaning |
|---|---|---|
| `totals`, `county`, `by-method` (no `ev_date`) | `cumulative` | Every stored day added together. `through_ev_date` is the latest day and `days` lists the days included. |
| `totals`, `county`, `by-method` with `?ev_date=YYYY-MM-DD` | `daily` | That day's voters only. |
| `by-day` | `daily_with_cumulative` | One entry per day with its own counts plus `cumulative_total`, `cumulative_in_person` and `cumulative_mail_in`. |
| `county-by-day` | `daily` | Per-county counts for each day. |

A day whose file is still being written is left out until it is complete, and `days` shows what was included. A complete day with no computed aggregates returns 503 instead of an undercount.

## Local Terraform checks (git hooks)

CI runs `terraform fmt -check`, `terraform validate` and `tflint`. To prove the first two before anything leaves your machine, install the repo's hooks once per clone:

```bash
bash scripts/install-git-hooks.sh
```

- **pre-commit** (plain `git commit`; GitButler's wrapper runs it as `pre-commit-user`) and **pre-push** (also run by `but push`) call `scripts/githooks/terraform-proof.sh`. It does nothing unless a `.tf`, `.tfvars`, `.terraform.lock.hcl` or `.tflint.hcl` file changed. The pre-push run also looks at everything committed since `origin/main`.
- Validation runs in a temporary directory of symlinks, so `terraform init` never rewrites the lock file or creates `.terraform/` in the repo. Provider downloads are cached in `~/.terraform.d/plugin-cache`; the first run can take a couple of minutes.
- GitButler's `but commit` does not run git hooks, so a commit made that way is only checked when you push (`but push --no-hooks` skips it) and again in CI. Run `bash scripts/githooks/terraform-proof.sh` by hand any time.

## Token scopes

Every minted consumer token has a scope. `aggregates` (the default, and every token minted before scopes existed) reads counts only. `roster` is the only scope that can read voter rows (names and VUIDs, see the roster routes). The shared Cloudflare Access service credential never grants roster access.

```bash
tx-turnout api generate --name "Analyst" --email analyst@example.org --scope roster
tx-turnout api list      # id, status, scope, created, expires, label
tx-turnout api revoke <token_id>
```

The first `generate` or `list` after this release adds the `scope` and `expires_at` columns to the token table (`aggregates` and no expiry by default); run `tx-turnout api list` once before deploying a Worker that reads them.

## Issuing API keys (expiring, stored in 1Password)

`tx-turnout api generate` replaces `tokens mint`. It needs the same `CLOUDFLARE_*` variables as above, plus the 1Password CLI (`op`) signed in (`op signin` in an interactive terminal; the CLI does not retry on an expired session).

Optional overrides: `TX_TURNOUT_OP_VAULT` (1Password vault id, default the `AD: Issued API Keys` vault) and `TX_TURNOUT_API_BASE_URL` (the URL written into each item's instructions, default `https://ev-api-preview.abstractdata.io`).

| Command | What it does |
| --- | --- |
| `api generate --name N --email E [--days 30] [--scope aggregates\|roster] [--show] [--non-interactive]` | Prompts for a missing name or email on a TTY. With `--non-interactive` or no TTY a missing option exits 2. Prints `token_id`, name, scope, `expires_at` and the 1Password item. The key prints only with `--show`. The email is never printed. |
| `api extend <token_id> [--days 30]` | New expiry is the later of now and the current expiry, plus N days. Updates D1, re-puts the KV entry with the new TTL, then updates the item's `Expires` and `Instructions` fields (found by tag `token:<token_id>`; a missing item warns). A revoked token is refused. A key that never expires is refused too unless you pass `--set-expiry`, which gives it now plus N. If the 1Password update fails after Cloudflare was extended, the command prints the new date to set by hand; do not run `extend` again, it would add the days a second time. |
| `api list` | `token_id`, status (`active`, `expired`, `revoked`), scope, created, expires (`never` for keys minted before expiry existed), label. |
| `api revoke <token_id>` | Unchanged: D1 `revoked_at` plus KV delete. The 1Password item is left in place. |

Exit codes: 0 ok, 1 service or auth error (Cloudflare or `op`), 2 usage or validation.

Write order: the key is generated locally, the 1Password item is created first, then the D1 row, then the KV entry. If D1 or KV fails, the item is archived (best effort), the partial token is revoked, and the command exits 1 naming only the Cloudflare error class. If 1Password fails nothing is written to Cloudflare. The key reaches `op` on stdin inside a JSON item template, never in argv.

1Password item: vault `qxyxd4oqdy5mpmg4t4qh3bfflm`, category API Credential, title `texas-turnout-scraper: <Name>`, tags `texas-turnout-scraper` and `token:<token_id>`. Fields: `credential` (the key, concealed), `Name`, `Email Address`, `Expires`, `Token ID`, `Instructions`. The email lives only in 1Password; D1 stores the name as the label.

Expiry: `consumer_api_token.expires_at` (ISO-8601 UTC, nullable). The KV entry is written with `expiration_ttl` (REST query parameter, minimum 60 seconds). The Worker keeps `SELECT *` and checks `expires_at` in Python: a non-empty value at or before now, or one that does not parse, is rejected; a missing column or empty value never expires. A KV hit is bounded by its TTL. Keys minted before this change have no expiry and never expire.

## Elections list and roster routes

All routes need `Authorization: Bearer <token>` (or `X-API-Key`). The roster routes need a token minted with `--scope roster`; an aggregates token gets 403 and the shared Access credential gets 401.

| Route | Who | Returns |
|---|---|---|
| `GET /elections?q=` | any valid credential | Stored elections, optionally filtered by id substring. Each has `first_ev_date`, `last_ev_date`, `total_rows` and `days` (`ev_date`, `rows`, `sha256`, `byte_length`). Counts only. |
| `GET /elections/{id}` | any valid credential | The same summary for one election (404 if unknown). |
| `GET /elections/{id}/roster/{YYYY-MM-DD}` | roster token | That day's stored CSV, byte for byte (`ID_VOTER`, `VOTER_NAME`, `VOTING_METHOD`, `PRECINCT`, `COUNTY`). `Content-Length` and `ETag` are set. |
| `GET /elections/{id}/roster` | roster token | Every day as one CSV: a single header with `EV_DATE` added as the first column, and each row dated. Streamed with no `Content-Length`. |

- **No pagination.** A day is one download, as the portal serves it. The all-days route is one longer download.
- **Incremental pulls.** Send `If-None-Match` with the last `ETag`. A match returns 304 and reads no rows. A day's `ETag` is its `sha256` from the list; the all-days `ETag` changes when any day changes.
- **Only complete, hash-verified days are served.** A day still loading is absent from the list and returns 404.
- **PII.** Responses carry names and VUIDs, are `Cache-Control: private, no-cache`, and are never stored in KV. Rows are not logged. A leaked roster token is revoked with `tx-turnout api revoke <id>`.
- The Worker reads each file in batches of 64 chunks (about 21 D1 queries for a 44 MB day).

## Precinct points and bounding boxes

`GET /elections/{id}/geo/precincts` returns a point and a bounding box for every roster `(county, precinct)` that matched a state precinct, so a consumer can attach them to each voter by joining on `COUNTY` and `PRECINCT`. The voterfile has no per-voter coordinates; precinct is the resolution. No voter data is in the response.

- **Who.** Any valid credential (Access service token, or a consumer token of any scope), the same gate as the aggregate routes. Without one: 401, before D1 or KV is read.
- **Body.** `source_election_id` (string), `version` (the election version in the ETag), `count`, `unmatched` (roster pairs with no state precinct) and `precincts`, sorted by county then precinct. Each entry has `county`, `precinct`, `pctkey`, `lat`, `lon` and `bbox` = `[min_lon, min_lat, max_lon, max_lat]` (`null` for rows loaded before boxes existed).
- **Caching.** Stored in KV under the election version with the standard TTL (24 h); `ETag` is `"v<version>"` and a matching `If-None-Match` returns 304. A geography reload alone does not refresh the cached body before the TTL or the next election version. KV propagation is about 60 s, which is unsuitable for election-night results; a faster path is out of scope.
- **Not loaded.** If the election's D1 has no geography tables, or `geo_area` has no box columns yet, the route returns 503 `{"detail": "geo_not_loaded"}` and caches nothing.
- **Loading.** Rebuild `data/geo/tx_geo.json` with `uv run python -m texas_turnout_scraper.geo_build`, then `uv run python -m texas_turnout_scraper.geo_load --election-id <id> --dry-run` and, once approved, `--apply`. The apply adds the four box columns to an existing `geo_area` and reloads because the file's sha256 changed. See `docs/adr/010-geography-tables.md`.

### Resuming and large downloads

- **Per-day downloads accept `Range`.** Responses carry `Accept-Ranges: bytes`. `Range: bytes=START-END`, `bytes=START-` and `bytes=-N` return `206` with `Content-Range`; a start past the end returns `416`. Several ranges, another unit, or a malformed header get the whole file. Send `If-Range: "<etag>"` so a resumed download is refused if the day changed in between (you get the full new file). `If-None-Match` still wins and returns `304`. Only the chunks covering the range are read.
- **To resume a stalled day:** note how many bytes you have, then request `Range: bytes=<have>-` with `If-Range` set to the ETag you started with, and append.
- **The all-days route does not support `Range`.** Pull days one at a time if you need resumable or parallel downloads.
- **Pipelined reads.** The Worker asks D1 for the next batch of chunks while it sends the current one, so one slow query no longer pauses the stream.
- **Timing.** A day response has `Server-Timing: lookup;dur=<ms>`, and the Worker logs the bytes and total milliseconds of each stream (counts only).
