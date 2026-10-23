# Private bucket of every version of each early-vote day file. Written only by the loader
# (bucket-scoped R2 S3 keys). No public domain, no r2.dev URL and no Worker binding.

variable "snapshot_bucket_name" {
  type        = string
  description = "Private R2 bucket holding versioned early-vote day files."
  default     = "tx-turnout-ev-snapshots-preview"

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$", var.snapshot_bucket_name))
    error_message = "snapshot_bucket_name must be a valid R2 bucket name."
  }
}

variable "snapshot_retention_days" {
  type        = number
  description = "Days a snapshot object is kept before R2 expires it."
  default     = 90

  validation {
    condition     = var.snapshot_retention_days >= 1
    error_message = "snapshot_retention_days must be at least 1."
  }
}

resource "cloudflare_r2_bucket" "ev_snapshots" {
  account_id = var.account_id
  name       = var.snapshot_bucket_name

  lifecycle {
    precondition {
      condition     = var.environment == "preview"
      error_message = "Refusing to manage the snapshot bucket outside preview."
    }

    prevent_destroy = true
  }
}

resource "cloudflare_r2_bucket_lifecycle" "ev_snapshots" {
  account_id  = var.account_id
  bucket_name = cloudflare_r2_bucket.ev_snapshots.name

  rules = [
    {
      id      = "expire-day-snapshots"
      enabled = true
      conditions = {
        prefix = "elections/"
      }
      delete_objects_transition = {
        condition = {
          type    = "Age"
          max_age = var.snapshot_retention_days * 86400
        }
      }
    },
    {
      id      = "abort-stale-multipart"
      enabled = true
      conditions = {
        prefix = ""
      }
      abort_multipart_uploads_transition = {
        condition = {
          type    = "Age"
          max_age = 86400
        }
      }
    },
  ]
}
