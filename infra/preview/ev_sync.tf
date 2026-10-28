# Early-vote sync Worker + Workflow (ADR-014). Ships inert with EV_SYNC_MODE=off.

locals {
  ev_sync_worker_name = "tx-turnout-ev-sync-preview"
}

resource "cloudflare_r2_bucket" "ev_staging" {
  account_id = var.account_id
  name       = "tx-turnout-ev-staging-preview"

  lifecycle {
    precondition {
      condition     = var.environment == "preview"
      error_message = "Refusing to create the EV staging bucket outside preview."
    }
  }
}

resource "cloudflare_r2_bucket_lifecycle" "ev_staging" {
  account_id  = var.account_id
  bucket_name = cloudflare_r2_bucket.ev_staging.name

  rules = [{
    id      = "ev-staging-expire-1-day"
    enabled = true
    conditions = {
      prefix = ""
    }
    delete_objects_transition = {
      condition = {
        type    = "Age"
        max_age = 86400
      }
    }
    abort_multipart_uploads_transition = {
      condition = {
        type    = "Age"
        max_age = 86400
      }
    }
  }]
}

resource "cloudflare_worker" "ev_sync" {
  account_id = var.account_id
  name       = local.ev_sync_worker_name
  logpush    = false
  tags       = ["preview"]

  observability = {
    enabled = true
    logs = {
      enabled            = true
      invocation_logs    = false
      head_sampling_rate = 1
    }
  }

  subdomain = {
    enabled          = false
    previews_enabled = false
  }
}

resource "cloudflare_workflow" "ev_sync" {
  account_id    = var.account_id
  workflow_name = "tx-turnout-ev-sync-preview"
  class_name    = local.ev_sync_workflow_class
  script_name   = cloudflare_worker.ev_sync.name

  default_retention = {
    success_retention = "1 day"
    error_retention   = "1 day"
  }

  lifecycle {
    precondition {
      condition     = var.environment == "preview"
      error_message = "Refusing to create the EV sync Workflow outside preview."
    }
  }
}

resource "cloudflare_worker_version" "ev_sync" {
  account_id          = var.account_id
  worker_id           = cloudflare_worker.ev_sync.id
  compatibility_date  = "2026-10-17"
  compatibility_flags = ["python_workers", "python_workflows"]
  main_module         = "entry.py"
  limits = {
    cpu_ms = 300000
  }
  deploy = false

  annotations = {
    workers_message = "ev-sync preview"
    workers_tag     = "preview"
  }

  modules = local.sync_worker_modules

  bindings = [
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
      type        = "r2_bucket"
      name        = "STAGING"
      bucket_name = cloudflare_r2_bucket.ev_staging.name
    },
    {
      type        = "d1"
      name        = "SCRATCH"
      database_id = cloudflare_d1_database.spike_scratch.id
    },
    {
      type          = "workflow"
      name          = "EV_SYNC"
      workflow_name = cloudflare_workflow.ev_sync.workflow_name
      class_name    = local.ev_sync_workflow_class
    },
    {
      type = "plain_text"
      name = "EV_SYNC_MODE"
      text = var.ev_sync_mode
    },
    {
      type = "plain_text"
      name = "EV_D1_BINDINGS"
      text = local.ev_d1_bindings
    },
  ]
}

resource "cloudflare_workers_deployment" "ev_sync" {
  account_id  = var.account_id
  script_name = cloudflare_worker.ev_sync.name
  strategy    = "percentage"

  annotations = {
    workers_message = "ev-sync preview"
  }

  versions = [{
    percentage = 100
    version_id = cloudflare_worker_version.ev_sync.id
  }]
}

# Cron moved off the read API Worker; GitHub dispatch binding on the read API is unchanged.
resource "cloudflare_workers_cron_trigger" "ev_sync" {
  account_id  = var.account_id
  script_name = cloudflare_worker.ev_sync.name
  schedules = [{
    cron = "0 15,16,22,23,3,4 * * *"
  }]

  depends_on = [cloudflare_workers_deployment.ev_sync]
}
