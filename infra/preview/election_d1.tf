# Election-scoped D1 databases are created by early-vote sync (ev-{source_election_id}).
# Preview imports the Nov 2026 general database; Terraform must not create or replace it.

variable "preview_election_d1_database_id" {
  type        = string
  description = "Cloudflare D1 database id for preview_source_election_id (ev-{id}). Import-only."
  default     = "d02a2da6-4b9b-4012-8302-134e4463e8d1"

  validation {
    condition     = can(regex("^[0-9a-fA-F-]{36}$", var.preview_election_d1_database_id))
    error_message = "preview_election_d1_database_id must be a UUID."
  }
}

import {
  to = cloudflare_d1_database.election_preview
  id = "${var.account_id}/${var.preview_election_d1_database_id}"
}

resource "cloudflare_d1_database" "election_preview" {
  account_id = var.account_id
  name       = "ev-${var.preview_source_election_id}"

  lifecycle {
    precondition {
      condition     = var.environment == "preview"
      error_message = "Refusing to manage election D1 outside preview."
    }

    # Existing ~112MB database: never recreate for hint/replication drift.
    ignore_changes = [
      name,
      primary_location_hint,
      read_replication,
    ]

    prevent_destroy = true
  }
}
