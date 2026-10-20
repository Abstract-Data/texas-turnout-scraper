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
  description = "source_election_id mapped to the preview D1 binding DB in EV_D1_BINDINGS."
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
