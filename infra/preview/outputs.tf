output "d1_database_id" {
  description = "Preview D1 database UUID. Per-election databases are not created here."
  value       = cloudflare_d1_database.preview.id
}

output "kv_namespace_id" {
  description = "Preview KV namespace id. Propagation is about 60 seconds and is not fit for election-night results."
  value       = cloudflare_workers_kv_namespace.preview.id
}

output "worker_name" {
  description = "Preview Python Worker script name."
  value       = cloudflare_worker.ev_api.name
}

output "worker_id" {
  description = "Immutable preview Worker id used by the Access destinations."
  value       = cloudflare_worker.ev_api.id
}

output "service_binding_name" {
  description = "Name consumers use when they bind to this Worker. The script name is worker_name."
  value       = "EV_API"
}

output "preview_api_url" {
  description = "HTTPS base URL for the routed preview read API (custom domain behind Access)."
  value       = local.preview_api_url
}

output "preview_api_hostname" {
  description = "Custom domain hostname attached to the preview Worker."
  value       = var.preview_api_hostname
}

output "workers_dev_url" {
  description = "Not routed. subdomain.enabled is false; use preview_api_url instead."
  value       = null
}

output "access_client_id" {
  description = "Send as CF-Access-Client-Id. Read with terraform output -raw. Do not log."
  value       = cloudflare_zero_trust_access_service_token.preview.client_id
  sensitive   = true
}

output "access_client_secret" {
  description = "Send as CF-Access-Client-Secret. Stored only in R2 state. Read with terraform output -raw. Do not log."
  value       = cloudflare_zero_trust_access_service_token.preview.client_secret
  sensitive   = true
}
