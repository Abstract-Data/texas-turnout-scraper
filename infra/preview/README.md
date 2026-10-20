# Preview Terraform

Preview/staging stack on the Abstract Data personal Cloudflare account. It creates one D1 shell database, one KV namespace, the Python Worker `tx-turnout-ev-api-preview`, and a Cloudflare Access service token. There is no production workspace.

The Worker has no `workers.dev` route and no preview or version URL. `subdomain.enabled` and `subdomain.previews_enabled` are both false. Nothing answers on a public hostname.

KV writes can take about 60 seconds to show up. That is not fit for election-night results. This module does not turn on KV instant mode.

The Worker module is a placeholder. It does not query D1 or KV and it does not log roster fields. The read API replaces `worker/entry.py` later. Consumers bind to this Worker as `EV_API`.

Python Workers need the `workers` SDK vendored into `worker/python_modules/` (via [Pywrangler](https://developers.cloudflare.com/workers/languages/python/packages/)). Terraform uploads `entry.py` plus that tree on `cloudflare_worker_version`. After changing `worker/pyproject.toml`, run:

```bash
cd infra/preview/worker
uv sync
uv run pywrangler sync
```

Commit `python_modules/` and `pylock.toml` with the change. CI re-runs `pywrangler sync` and fails if those paths drift.

Pull requests run `terraform fmt`, `terraform validate`, and `tflint` only. They do not read secrets and they do not plan or apply.

A push to `main` or `workflow_dispatch` on `main` runs two jobs in the GitHub Environment `preview`. Plan is John's first approval. Apply is the second. John sees the plan before anything applies. Do not run `terraform plan` or `terraform apply` on a laptop.

## Bootstrap

Do the GitHub settings before adding secrets. Do not try to apply from this pull request.

1. In the Cloudflare dashboard, confirm the account is the Abstract Data personal account, not the Quorum Report account.
2. Create a private R2 bucket before the first apply. Terraform cannot create the bucket it stores state in. Suggested name: `tx-turnout-tfstate-preview`. Leave public access off. Dashboard: R2 → Create bucket. Equivalent command, already authenticated to that account: `npx wrangler r2 bucket create tx-turnout-tfstate-preview`.
3. Create an R2 API token with Object Read & Write on that bucket only. Store the Access Key ID and Secret Access Key. These are S3 credentials, separate from `CLOUDFLARE_API_TOKEN`. See [R2 API tokens](https://developers.cloudflare.com/r2/api/tokens/). The S3 endpoint is `https://<ACCOUNT_ID>.r2.cloudflarestorage.com`. The region is `auto`. See [R2 S3 API](https://developers.cloudflare.com/r2/api/s3/api/).
4. In GitHub, open Settings → Environments → `preview` for `Abstract-Data/texas-turnout-scraper`. This is a settings step, not something this repository configures.
   - Deployment branches: selected branch `main` only.
   - Required reviewers: at least one.
5. Add these four values as Environment secrets on `preview`. Do not add them as repository secrets. `CLOUDFLARE_API_TOKEN`, `R2_ACCESS_KEY_ID`, and `R2_SECRET_ACCESS_KEY` are not GitHub secrets.

| `preview` environment secret | Value |
| --- | --- |
| `CLOUDFLARE_ACCOUNT_ID` | Abstract Data personal account id, 32 characters. |
| `TF_STATE_BUCKET` | State bucket name, such as `tx-turnout-tfstate-preview`. |
| `TF_STATE_ENDPOINT` | `https://<ACCOUNT_ID>.r2.cloudflarestorage.com` |
| `OP_SERVICE_ACCOUNT_TOKEN` | 1Password service account token that can read Environment `s6tljdcafkgqxfuxhcnj3m5x5a`. |

6. In that 1Password Environment, name these variables exactly:

| 1Password variable | Value |
| --- | --- |
| `CLOUDFLARE_API_TOKEN` | API token. Scopes below. |
| `R2_ACCESS_KEY_ID` | R2 S3 access key id for the state bucket. |
| `R2_SECRET_ACCESS_KEY` | R2 S3 secret access key for the state bucket. |
| `CLOUDFLARE_KV_NAMESPACE_ID` | Add after the first apply. Copy the `kv_namespace_id` output. |

`CLOUDFLARE_API_TOKEN` account scopes, limited to the Abstract Data personal account. No zone scopes. No Quorum Report account. No Workers R2 Storage permission (state uses the R2 S3 keys):

- Account → D1 → Edit
- Account → Workers KV Storage → Edit
- Account → Workers Scripts → Edit
- Account → Access: Apps and Policies → Write
- Account → Access: Service Tokens → Write

Permission names: [Cloudflare API token permissions](https://developers.cloudflare.com/fundamentals/api/reference/permissions/).

Plan and apply load the 1Password variables with `op run --environment`. See [Read environment variables with op run](https://www.1password.dev/environments/read-environment-variables). `load-secrets-action` cannot load an Environment by id with a service account token. That whole-environment load is Workload Identity only (`OP_ENVIRONMENT_ID`), and the action README marks it as public preview. See [load-secrets-action](https://github.com/1Password/load-secrets-action/blob/main/README.md).

After those settings and secrets exist, a push to `main` waits for two `preview` approvals. The plan job is the first. It runs `terraform init` and `terraform plan -out=tfplan`, writes `terraform show -no-color tfplan` to the job summary, and stores the saved plan in the private state bucket at `plans/<run_id>-<attempt>/tfplan` plus `plans/<run_id>-<attempt>/tfplan.sha256`. The repository is public, so the plan file is not a GitHub artifact. The apply job is the second approval. It downloads that object, checks the SHA-256 against the plan job output, and runs `terraform apply -input=false tfplan`. It does not create a new plan. After a successful apply it deletes both objects. Do not point this workflow at a production environment.

After the first successful apply, the Access client id and secret are in the R2 state file. The dashboard does not show the secret again. On a trusted machine, with `OP_SERVICE_ACCOUNT_TOKEN` set and `preview.auto.tfvars` holding `account_id`, read them through the same `op run` wrapper. Do not print the token or the R2 keys.

```bash
cd infra/preview
op run --environment s6tljdcafkgqxfuxhcnj3m5x5a -- bash -c 'AWS_ACCESS_KEY_ID="$R2_ACCESS_KEY_ID" AWS_SECRET_ACCESS_KEY="$R2_SECRET_ACCESS_KEY" terraform output -raw access_client_id'
op run --environment s6tljdcafkgqxfuxhcnj3m5x5a -- bash -c 'AWS_ACCESS_KEY_ID="$R2_ACCESS_KEY_ID" AWS_SECRET_ACCESS_KEY="$R2_SECRET_ACCESS_KEY" terraform output -raw access_client_secret'
```

`backend.hcl` is gitignored and contains only:

```hcl
bucket    = "<TF_STATE_BUCKET>"
endpoints = { s3 = "<TF_STATE_ENDPOINT>" }
```

Send the id as `CF-Access-Client-Id` and the secret as `CF-Access-Client-Secret`. Do not log either value.
