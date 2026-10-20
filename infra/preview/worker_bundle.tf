locals {
  preview_worker_dir = "${path.module}/worker"

  preview_worker_entry_module = {
    name         = "entry.py"
    content_type = "text/x-python"
    content_file = "${local.preview_worker_dir}/entry.py"
  }

  # pywrangler sync vendors workers-runtime-sdk (workers-py >= 1.9) here.
  # Wrangler uploads these as buffer modules (application/octet-stream).
  preview_worker_vendor_paths = sort([
    for rel in fileset("${local.preview_worker_dir}/python_modules", "**") :
    rel if !endswith(rel, "/")
  ])

  preview_worker_vendor_modules = [
    for rel in local.preview_worker_vendor_paths : {
      name         = "python_modules/${rel}"
      content_type = "application/octet-stream"
      content_file = "${local.preview_worker_dir}/python_modules/${rel}"
    }
  ]

  preview_worker_modules = concat(
    [local.preview_worker_entry_module],
    local.preview_worker_vendor_modules,
  )
}
