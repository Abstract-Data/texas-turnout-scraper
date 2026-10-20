locals {
  preview_worker_dir = "${path.module}/worker"

  preview_worker_entry_module = {
    name         = "entry.py"
    content_type = "text/x-python"
    content_file = "${local.preview_worker_dir}/entry.py"
  }

  # pywrangler sync vendors workers-runtime-sdk here (see worker/pylock.toml).
  # Wrangler: python_modules/workers/*.{js,mjs} are ES modules; other vendor paths are buffer blobs.
  preview_worker_vendor_paths = sort([
    for rel in fileset("${local.preview_worker_dir}/python_modules", "**") :
    rel if !endswith(rel, "/")
  ])

  preview_worker_vendor_modules = [
    for rel in local.preview_worker_vendor_paths : {
      name = "python_modules/${rel}"
      content_type = (
        startswith(rel, "workers/") && (endswith(rel, ".js") || endswith(rel, ".mjs"))
      ) ? "application/javascript+module" : "application/octet-stream"
      content_file = "${local.preview_worker_dir}/python_modules/${rel}"
    }
  ]

  preview_worker_app_paths = sort([
    for rel in fileset("${local.preview_worker_dir}/src/ev_api", "**") :
    rel if endswith(rel, ".py")
  ])

  preview_worker_app_modules = [
    for rel in local.preview_worker_app_paths : {
      name         = "ev_api/${rel}"
      content_type = "text/x-python"
      content_file = "${local.preview_worker_dir}/src/ev_api/${rel}"
    }
  ]

  preview_worker_modules = concat(
    [local.preview_worker_entry_module],
    local.preview_worker_app_modules,
    local.preview_worker_vendor_modules,
  )
}
