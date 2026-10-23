#!/usr/bin/env bash
# Prove `terraform fmt -check` and `terraform validate` pass when Terraform files changed.
#
#   terraform-proof.sh             changes in the working tree, index and untracked files
#   terraform-proof.sh --base REF  also everything committed since REF (used by pre-push)
#
# Validation runs in a temporary directory of symlinks, so `terraform init` never rewrites
# .terraform.lock.hcl or creates .terraform/ in the repo. Set TF_PROOF_DIRS (colon separated)
# to change the module roots; the default is infra/preview.
set -euo pipefail

base=""
if [[ "${1:-}" == "--base" ]]; then
  base="${2:?--base needs a ref}"
fi

root="$(git rev-parse --show-toplevel)"
cd "${root}"
IFS=: read -r -a roots <<<"${TF_PROOF_DIRS:-infra/preview}"
pattern='(\.tf|\.tfvars|\.tf\.json)$|(^|/)\.terraform\.lock\.hcl$|(^|/)\.tflint\.hcl$'

changed="$(
  {
    git diff --name-only HEAD 2>/dev/null || true
    git diff --name-only --cached 2>/dev/null || true
    git ls-files --others --exclude-standard
    if [[ -n "${base}" ]]; then
      git diff --name-only "${base}"...HEAD 2>/dev/null || true
    fi
  } | sort -u | grep -E "${pattern}" || true
)"

if [[ -z "${changed}" ]]; then
  exit 0
fi

targets=()
while IFS= read -r file; do
  matched=""
  for module in "${roots[@]}"; do
    if [[ "${file}" == "${module}/"* ]]; then
      matched="${module}"
    fi
  done
  targets+=("${matched:-$(dirname "${file}")}")
done <<<"${changed}"

command -v terraform >/dev/null 2>&1 || {
  echo "BLOCKED: Terraform files changed but terraform is not installed:" >&2
  echo "${changed}" >&2
  exit 1
}

status=0
for dir in $(printf '%s\n' "${targets[@]}" | sort -u); do
  echo "terraform fmt -check -recursive ${dir}"
  if ! terraform -chdir="${dir}" fmt -check -recursive -diff; then
    echo "BLOCKED: terraform fmt failed in ${dir}. Run: terraform -chdir=${dir} fmt -recursive" >&2
    status=1
    continue
  fi

  work="$(mktemp -d)"
  (
    shopt -s nullglob dotglob
    for entry in "${root}/${dir}"/*; do
      name="$(basename "${entry}")"
      case "${name}" in
        .terraform | .git | .terraform.lock.hcl) continue ;;
      esac
      ln -s "${entry}" "${work}/${name}"
    done
    if [[ -f "${root}/${dir}/.terraform.lock.hcl" ]]; then
      cp "${root}/${dir}/.terraform.lock.hcl" "${work}/.terraform.lock.hcl"
    fi
  )
  export TF_PLUGIN_CACHE_DIR="${TF_PLUGIN_CACHE_DIR:-${HOME}/.terraform.d/plugin-cache}"
  mkdir -p "${TF_PLUGIN_CACHE_DIR}"
  echo "terraform validate ${dir}"
  if ! terraform -chdir="${work}" init -backend=false -input=false -no-color >"${work}/.init.log" 2>&1; then
    cat "${work}/.init.log" >&2
    echo "BLOCKED: terraform init (backend disabled) failed for ${dir}." >&2
    status=1
  elif ! terraform -chdir="${work}" validate -no-color; then
    echo "BLOCKED: terraform validate failed in ${dir}." >&2
    status=1
  fi
  rm -rf "${work}"
done

if [[ "${status}" -eq 0 ]]; then
  echo "terraform fmt and validate: proven for $(printf '%s ' "${targets[@]}" | tr ' ' '\n' | sort -u | tr '\n' ' ')"
fi
exit "${status}"
