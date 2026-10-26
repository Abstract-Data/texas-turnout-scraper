# Temporary: the second spike for the early-vote Workflow ingest (ADR-014).
#
# Everything here is named "spike" so the whole thing deletes in one commit: this file,
# the `local.spike_bindings` reference and the `python_workflows` flag in main.tf, and
# worker/src/ev_api/spike_workflow.py plus its mount lines.
#
# Provider schema verified against cloudflare/cloudflare 5.28.0 (the pinned version in
# .terraform.lock.hcl) with `terraform providers schema -json`, not from memory.
#
# main.tf carries two lines of this spike. `python_workflows` joins compatibility_flags,
# because Python Workflows need both it and `python_workers` plus compatibility_date
# 2025-08-01 or later; the Worker is already on 2026-10-17. And `local.spike_bindings`
# joins the worker version's `bindings` concat.

locals {
  # The Workflow binding's class_name must equal the module-level name in worker/entry.py.
  spike_workflow_class = "SpikeWorkflow"

  spike_bindings = [
    {
      type        = "r2_bucket"
      name        = "STAGING"
      bucket_name = cloudflare_r2_bucket.spike_staging.name
    },
    {
      type        = "d1"
      name        = "SCRATCH"
      database_id = cloudflare_d1_database.spike_scratch.id
    },
    {
      type          = "workflow"
      name          = "SPIKE_WORKFLOW"
      workflow_name = cloudflare_workflow.spike_ingest.workflow_name
      class_name    = local.spike_workflow_class
    },
  ]
}

# Private staging bucket for the canonical day object and the per-range segments.
# Private is the default: this module declares no cloudflare_r2_managed_domain (r2.dev)
# and no cloudflare_r2_custom_domain, which are the only two ways a bucket is served
# publicly. The objects hold voter names, so nothing may make this bucket public.
resource "cloudflare_r2_bucket" "spike_staging" {
  account_id = var.account_id
  name       = "tx-turnout-ev-staging-spike"

  lifecycle {
    precondition {
      condition     = var.environment == "preview"
      error_message = "Refusing to create the spike staging bucket outside preview."
    }
  }
}

# Owner decision 4: delete at commit, with a 1-day lifecycle and abort-incomplete-multipart
# as the backstop. `max_age` is seconds (R2 lifecycle API: "an age in seconds"), and the
# default abort for incomplete multipart uploads is 7 days, which is too long for PII.
resource "cloudflare_r2_bucket_lifecycle" "spike_staging" {
  account_id  = var.account_id
  bucket_name = cloudflare_r2_bucket.spike_staging.name

  rules = [{
    id      = "spike-expire-1-day"
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

# Owner decision 7: the range-write test runs against a scratch database first. This is
# never added to EV_D1_BINDINGS, so the read API cannot reach it.
resource "cloudflare_d1_database" "spike_scratch" {
  account_id            = var.account_id
  name                  = "tx-turnout-ev-scratch"
  primary_location_hint = var.primary_location_hint

  lifecycle {
    precondition {
      condition     = var.environment == "preview"
      error_message = "Refusing to create the spike scratch database outside preview."
    }
  }
}

# R5 is answered: the provider declares both the Workflow and its class. The Workflow
# points at the Worker by name, so it does not depend on a version; the version's
# SPIKE_WORKFLOW binding then depends on this resource.
resource "cloudflare_workflow" "spike_ingest" {
  account_id    = var.account_id
  workflow_name = "tx-turnout-ev-spike"
  class_name    = local.spike_workflow_class
  script_name   = cloudflare_worker.ev_api.name

  # Step outputs persist for the account default otherwise (3 days Free, 30 days Paid).
  # ADR-014 Privacy keeps it as short as allowed.
  default_retention = {
    success_retention = "1 day"
    error_retention   = "1 day"
  }

  lifecycle {
    precondition {
      condition     = var.environment == "preview"
      error_message = "Refusing to create the spike Workflow outside preview."
    }
  }
}
