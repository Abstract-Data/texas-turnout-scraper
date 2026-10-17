terraform {
  required_version = ">= 1.10.0"

  required_providers {
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 5.0"
    }
  }

  # Partial backend. bucket and endpoints.s3 come from CI backend config
  # (TF_STATE_BUCKET, TF_STATE_ENDPOINT). R2 is S3-compatible and has no STS,
  # IAM, or EC2 metadata API.
  # https://developer.hashicorp.com/terraform/language/backend/s3
  # https://developers.cloudflare.com/r2/api/s3/api/
  backend "s3" {
    key                         = "preview/terraform.tfstate"
    region                      = "auto"
    use_path_style              = true
    skip_credentials_validation = true
    skip_region_validation      = true
    skip_requesting_account_id  = true
    skip_metadata_api_check     = true
    skip_s3_checksum            = true
  }
}

# Token is CLOUDFLARE_API_TOKEN. Do not set api_token in this file.
# https://github.com/cloudflare/terraform-provider-cloudflare/blob/main/docs/index.md
provider "cloudflare" {}
