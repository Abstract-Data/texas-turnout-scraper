variable "account_id" {
  type        = string
  description = "Abstract Data personal Cloudflare account ID. Injected from the CLOUDFLARE_ACCOUNT_ID repo secret. Do not hardcode it."
  nullable    = false

  validation {
    condition     = length(var.account_id) == 32 && can(regex("^[0-9a-fA-F]+$", var.account_id))
    error_message = "account_id must be the 32-character Cloudflare account id."
  }
}

variable "environment" {
  type        = string
  description = "Only the preview stack exists in this module."
  default     = "preview"

  validation {
    condition     = var.environment == "preview"
    error_message = "This module refuses any environment other than preview."
  }
}

variable "preview_source_election_id" {
  type        = string
  description = "source_election_id mapped to the imported election D1 binding in EV_D1_BINDINGS."
  default     = "53815"

  validation {
    condition     = can(regex("^[0-9]+$", var.preview_source_election_id))
    error_message = "preview_source_election_id must be digits only."
  }
}

variable "primary_location_hint" {
  type        = string
  description = "D1 primary location hint for the preview shell database. enam is the US hint; it is not a residency lock."
  default     = "enam"

  validation {
    condition     = contains(["wnam", "enam", "weur", "eeur", "apac", "oc"], var.primary_location_hint)
    error_message = "primary_location_hint must be a D1 location hint."
  }
}

variable "preview_zone_name" {
  type        = string
  description = "Cloudflare zone on the Abstract Data personal account that hosts the preview API custom domain. Not the Quorum Report account."
  default     = "abstractdata.io"

  validation {
    condition     = can(regex("^[a-z0-9.-]+\\.[a-z]{2,}$", var.preview_zone_name))
    error_message = "preview_zone_name must be a DNS zone name (e.g. abstractdata.io)."
  }
}

variable "preview_api_hostname" {
  type        = string
  description = "Custom domain hostname routed to the preview Worker. Cloudflare Access and the Worker app auth apply on this host."
  default     = "ev-api-preview.abstractdata.io"

  validation {
    condition     = can(regex("^[a-z0-9.-]+$", var.preview_api_hostname))
    error_message = "preview_api_hostname must be a hostname without a scheme or path."
  }
}

variable "ev_sync_mode" {
  type        = string
  description = "Kill switch for the EV sync Worker Workflow: off, dry_run, or apply. Default off so merge applies inert."
  default     = "off"

  validation {
    condition     = contains(["off", "dry_run", "apply"], var.ev_sync_mode)
    error_message = "ev_sync_mode must be off, dry_run, or apply."
  }
}

variable "github_dispatch_token" {
  type        = string
  description = "Fine-grained GitHub token with only Actions: write on Abstract-Data/texas-turnout-scraper. The Worker cron uses it to dispatch early-vote-sync.yml. Empty leaves the binding off and the cron skips. From the EV_DISPATCH_GITHUB_TOKEN preview environment secret."
  default     = ""
  sensitive   = true
  nullable    = false

  validation {
    condition     = var.github_dispatch_token == "" || can(regex("^[^[:space:]]+$", var.github_dispatch_token))
    error_message = "github_dispatch_token must be empty or a token without whitespace."
  }
}
