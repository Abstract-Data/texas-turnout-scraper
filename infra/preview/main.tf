# Preview/staging only, on the Abstract Data personal Cloudflare account.
# No production worker, database, namespace, or Access application.
#
# KV writes are eventually consistent. About 60 seconds of propagation is not
# fit for election-night results. This module does not select KV instant mode
# and does not build a faster path.

locals {
  worker_name = "tx-turnout-ev-api-preview"
  # Worker binding name for the imported election D1 (see election_d1.tf).
  preview_election_d1_binding = "EV_${var.preview_source_election_id}"
  # Preview sync targets Nov 2026 general (see early-vote-sync workflow default).
  ev_d1_bindings = jsonencode({
    (var.preview_source_election_id) = local.preview_election_d1_binding
  })
  preview_api_url = "https://${var.preview_api_hostname}"
  preview_zone_id = data.cloudflare_zones.preview.result[0].id
}

data "cloudflare_zones" "preview" {
  account = {
    id = var.account_id
  }
  name = var.preview_zone_name
}

resource "cloudflare_d1_database" "preview" {
  account_id            = var.account_id
  name                  = "tx-turnout-preview"
  primary_location_hint = var.primary_location_hint

  # Shell database for the preview Worker binding. The writer creates one D1
  # database per election and must hard-fail above 10 GB. This is not an
  # election database.
  lifecycle {
    precondition {
      condition     = var.environment == "preview"
      error_message = "Refusing to create a D1 database outside preview."
    }
  }
}

resource "cloudflare_workers_kv_namespace" "preview" {
  account_id = var.account_id
  title      = "tx-turnout-preview"
}

resource "cloudflare_worker" "ev_api" {
  account_id = var.account_id
  name       = local.worker_name
  logpush    = false
  tags       = ["preview"]

  # Preview-only: Workers Logs for app console/structured output (see README).
  # invocation_logs stays false — this API accepts Access + bearer/API-key headers.
  observability = {
    enabled = true
    logs = {
      enabled            = true
      invocation_logs    = false
      head_sampling_rate = 1
    }
  }

  # workers.dev stays off (no *.workers.dev route). Traffic uses the custom
  # domain in cloudflare_workers_custom_domain.preview. previews_enabled stays
  # false so version/preview URLs are not routed.
  subdomain = {
    enabled          = false
    previews_enabled = false
  }
}

resource "cloudflare_workers_custom_domain" "preview" {
  account_id = var.account_id
  hostname   = var.preview_api_hostname
  service    = cloudflare_worker.ev_api.name
  zone_id    = local.preview_zone_id

  lifecycle {
    precondition {
      condition     = length(data.cloudflare_zones.preview.result) == 1
      error_message = "Expected exactly one zone named ${var.preview_zone_name} on account ${var.account_id}."
    }
  }
}

resource "cloudflare_worker_version" "preview" {
  account_id          = var.account_id
  worker_id           = cloudflare_worker.ev_api.id
  compatibility_date  = "2026-10-17"
  compatibility_flags = ["python_workers"]
  main_module         = "entry.py"
  # The deployment resource below publishes this version. Leaving deploy false
  # avoids a second, unannotated rollout.
  deploy = false

  annotations = {
    workers_message = "preview placeholder"
    workers_tag     = "preview"
  }

  modules = local.preview_worker_modules

  # GITHUB_DISPATCH_TOKEN is bound only once the owner sets it; until then the cron
  # handler logs and skips (docs/adr/013-worker-cron-dispatches-sync.md).
  bindings = concat([
    {
      type        = "d1"
      name        = "DB"
      database_id = cloudflare_d1_database.preview.id
    },
    {
      type        = "d1"
      name        = local.preview_election_d1_binding
      database_id = cloudflare_d1_database.election_preview.id
    },
    {
      type         = "kv_namespace"
      name         = "EV"
      namespace_id = cloudflare_workers_kv_namespace.preview.id
    },
    {
      type = "plain_text"
      name = "ACCESS_CLIENT_ID"
      text = cloudflare_zero_trust_access_service_token.preview.client_id
    },
    {
      type = "secret_text"
      name = "ACCESS_CLIENT_SECRET"
      text = cloudflare_zero_trust_access_service_token.preview.client_secret
    },
    {
      type = "plain_text"
      name = "EV_D1_BINDINGS"
      text = local.ev_d1_bindings
    },
    ], var.github_dispatch_token == "" ? [] : [
    {
      type = "secret_text"
      name = "GITHUB_DISPATCH_TOKEN"
      text = var.github_dispatch_token
    },
  ])
}

resource "cloudflare_workers_deployment" "preview" {
  account_id  = var.account_id
  script_name = cloudflare_worker.ev_api.name
  strategy    = "percentage"

  annotations = {
    workers_message = "preview only"
  }

  versions = [{
    percentage = 100
    version_id = cloudflare_worker_version.preview.id
  }]
}

# 10:00, 17:00 and 22:00 America/Chicago in both CDT and CST, as UTC hours. The handler
# (worker/src/ev_api/cron.py CRON_SCHEDULE) keeps the three that match the date and stops
# after 11:59 PM Central on 2026-11-03. It only dispatches the early-vote-sync workflow.
resource "cloudflare_workers_cron_trigger" "ev_sync" {
  account_id  = var.account_id
  script_name = cloudflare_worker.ev_api.name
  schedules = [{
    cron = "0 15,16,22,23,3,4 * * *"
  }]

  depends_on = [cloudflare_workers_deployment.preview]
}

resource "cloudflare_zero_trust_access_service_token" "preview" {
  account_id = var.account_id
  name       = local.worker_name
  duration   = "8760h"

  # Rotate the secret through Terraform so state and the Worker's ACCESS_CLIENT_SECRET
  # binding learn the new value. A dashboard rotation leaves state stale because the API
  # never returns the secret again. Raising the version makes the provider rotate and read
  # the new secret. Cloudflare was at version 2 after a manual rotation, so use 3.
  # The previous secret stays valid until the timestamp, which gives time to update
  # 1Password from `terraform output -raw access_client_secret`.
  client_secret_version             = 3
  previous_client_secret_expires_at = "2026-10-24T06:00:00Z"
}

resource "cloudflare_zero_trust_access_policy" "preview_service" {
  account_id = var.account_id
  name       = "${local.worker_name}-service-auth"
  decision   = "non_identity"

  include = [{
    service_token = {
      token_id = cloudflare_zero_trust_access_service_token.preview.id
    }
  }]
}

resource "cloudflare_zero_trust_access_application" "preview" {
  account_id                = var.account_id
  name                      = local.worker_name
  type                      = "self_hosted"
  app_launcher_visible      = false
  auto_redirect_to_identity = false
  service_auth_401_redirect = true

  destinations = [
    {
      type      = "worker"
      worker_id = cloudflare_worker.ev_api.id
    },
    {
      type = "public"
      uri  = var.preview_api_hostname
    },
  ]

  policies = [{
    id         = cloudflare_zero_trust_access_policy.preview_service.id
    precedence = 1
  }]
}
