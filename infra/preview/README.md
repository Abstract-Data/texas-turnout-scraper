# Preview Terraform

Preview/staging stack on the Abstract Data personal Cloudflare account. It creates one D1 shell database, one KV namespace, the Python Worker `tx-turnout-ev-api-preview`, and a Cloudflare Access service token. There is no production workspace.

Apply only from the `Terraform preview` GitHub Actions workflow. Do not run `terraform apply` on a laptop.

KV writes can take about 60 seconds to show up. That is not fit for election-night results. This module does not turn on KV instant mode.

The Worker module is a placeholder. It does not query D1 or KV and it does not log roster fields. The read API replaces `worker/entry.py` later. Consumers bind to this Worker as `EV_API`.

## Bootstrap the state bucket

Do this once, on the Abstract Data personal account, before the first CI apply. Terraform cannot create the bucket it stores state in.

1. In the Cloudflare dashboard, confirm the account is the Abstract Data personal account, not the Quorum Report account.
2. Create a private R2 bucket. Suggested name: `tx-turnout-tfstate-preview`. Leave public access off. Dashboard: R2 → Create bucket. Equivalent command, already authenticated to that account: `npx wrangler r2 bucket create tx-turnout-tfstate-preview`.
3. Create an R2 API token with Object Read & Write on that bucket only. Store the Access Key ID and Secret Access Key. These are S3 credentials, separate from `CLOUDFLARE_API_TOKEN`. See [R2 API tokens](https://developers.cloudflare.com/r2/api/tokens/).
4. The S3 endpoint is `https://<ACCOUNT_ID>.r2.cloudflarestorage.com`. The region is `auto`. See [R2 S3 API](https://developers.cloudflare.com/r2/api/s3/api/).

## GitHub repo secrets

Add these on `Abstract-Data/texas-turnout-scraper` under Settings → Secrets and variables → Actions. Do not commit the values.

| Secret | Value |
| --- | --- |
| `CLOUDFLARE_API_TOKEN` | API token on the Abstract Data personal account. Scopes below. |
| `CLOUDFLARE_ACCOUNT_ID` | That account's 32-character id. |
| `R2_ACCESS_KEY_ID` | R2 S3 access key id for the state bucket. |
| `R2_SECRET_ACCESS_KEY` | R2 S3 secret access key for the state bucket. |
| `TF_STATE_BUCKET` | State bucket name, such as `tx-turnout-tfstate-preview`. |
| `TF_STATE_ENDPOINT` | `https://<ACCOUNT_ID>.r2.cloudflarestorage.com` |

`CLOUDFLARE_API_TOKEN` account scopes, limited to the Abstract Data personal account. No zone scopes. No Quorum Report account. No Workers R2 Storage permission (state uses the R2 S3 keys):

- Account → D1 → Edit
- Account → Workers KV Storage → Edit
- Account → Workers Scripts → Edit
- Account → Access: Apps and Policies → Write
- Account → Access: Service Tokens → Write

Permission names: [Cloudflare API token permissions](https://developers.cloudflare.com/fundamentals/api/reference/permissions/).

The workflow uses the GitHub Environment `preview`. Add required reviewers there if you want a human gate. Do not point this workflow at a production environment.

After the first successful apply, the Access client id and secret are in the R2 state file. The dashboard does not show the secret again. On a trusted machine, with the same backend secrets in the environment:

```bash
cd infra/preview
export AWS_ACCESS_KEY_ID="$R2_ACCESS_KEY_ID"
export AWS_SECRET_ACCESS_KEY="$R2_SECRET_ACCESS_KEY"
export TF_VAR_account_id="$CLOUDFLARE_ACCOUNT_ID"
terraform init -input=false -backend-config=backend.hcl
terraform output -raw access_client_id
terraform output -raw access_client_secret
```

`backend.hcl` is gitignored and contains only:

```hcl
bucket   = "<TF_STATE_BUCKET>"
endpoints = { s3 = "<TF_STATE_ENDPOINT>" }
```

Send the id as `CF-Access-Client-Id` and the secret as `CF-Access-Client-Secret`. Do not log either value.
