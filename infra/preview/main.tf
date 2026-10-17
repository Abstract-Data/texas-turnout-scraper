# Preview/staging only, on the Abstract Data personal Cloudflare account.
# No production worker, database, namespace, or Access application.
#
# KV writes are eventually consistent. About 60 seconds of propagation is not
# fit for election-night results. This module does not select KV instant mode
# and does not build a faster path.

locals {
  worker_name = "tx-turnout-ev-api-preview"
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

  observability = {
    enabled = false
  }

  # workers.dev is off. Wrangler names this workers_dev; provider v5.28.0
  # sets it with subdomain.enabled. Disabling that route does not disable
  # preview or version URLs, so subdomain.previews_enabled is false too.
  # No workers.dev, preview, or version hostname is routed. The Access
  # application below is attached for a later route change; this resource
  # does not enable one.
  subdomain = {
    enabled          = false
    previews_enabled = false
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

  modules = [{
    name         = "entry.py"
    content_type = "text/x-python"
    content_file = "${path.module}/worker/entry.py"
  }]

  bindings = [
    {
      type        = "d1"
      name        = "DB"
      database_id = cloudflare_d1_database.preview.id
    },
    {
      type         = "kv_namespace"
      name         = "EV"
      namespace_id = cloudflare_workers_kv_namespace.preview.id
    },
  ]
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

resource "cloudflare_zero_trust_access_service_token" "preview" {
  account_id = var.account_id
  name       = local.worker_name
  duration   = "8760h"
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
      type      = "preview_worker"
      worker_id = cloudflare_worker.ev_api.id
    },
  ]

  policies = [{
    id         = cloudflare_zero_trust_access_policy.preview_service.id
    precedence = 1
  }]
}
