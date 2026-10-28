locals {
  sync_worker_dir = "${path.module}/sync_worker"
  # Reuse the read API Worker vendored tree until sync_worker has its own pywrangler lock.
  sync_worker_vendor_paths = sort([
    for rel in fileset("${local.preview_worker_dir}/python_modules", "**") :
    rel if !endswith(rel, "/")
  ])

  sync_worker_entry_module = {
    name         = "entry.py"
    content_type = "text/x-python"
    content_file = "${local.sync_worker_dir}/entry.py"
  }

  sync_worker_vendor_modules = [
    for rel in local.sync_worker_vendor_paths : {
      name = "python_modules/${rel}"
      content_type = (
        startswith(rel, "workers/") && (endswith(rel, ".js") || endswith(rel, ".mjs"))
      ) ? "application/javascript+module" : "application/octet-stream"
      content_file = "${local.preview_worker_dir}/python_modules/${rel}"
    }
  ]

  sync_worker_app_paths = sort([
    for rel in fileset("${local.sync_worker_dir}/src/ev_sync", "**") :
    rel if endswith(rel, ".py")
  ])

  sync_worker_app_modules = [
    for rel in local.sync_worker_app_paths : {
      name         = "ev_sync/${rel}"
      content_type = "text/x-python"
      content_file = "${local.sync_worker_dir}/src/ev_sync/${rel}"
    }
  ]

  sync_worker_modules = concat(
    [local.sync_worker_entry_module],
    local.sync_worker_app_modules,
    local.sync_worker_vendor_modules,
  )

  ev_sync_workflow_class = "EvSyncWorkflow"
}
