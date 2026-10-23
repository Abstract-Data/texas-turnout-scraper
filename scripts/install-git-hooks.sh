#!/usr/bin/env bash
# Install the repo's git hooks into this clone. Safe to run again.
#
#   pre-commit  terraform-proof.sh: fmt and validate when Terraform files changed
#   pre-push    the same check, including everything committed since origin/main
#
# GitButler owns .git/hooks/pre-commit and runs `pre-commit-user` first, so the commit hook is
# installed under that name when GitButler's wrapper is present. The pre-push hook belongs to
# git-lfs here, so the check is added to the top of it between markers instead of replacing it.
set -euo pipefail

root="$(git rev-parse --show-toplevel)"
hooks="$(git rev-parse --git-path hooks)"
case "${hooks}" in /*) ;; *) hooks="${root}/${hooks}" ;; esac
proof="${root}/scripts/githooks/terraform-proof.sh"
marker="# terraform-proof-hook"

commit_hook="${hooks}/pre-commit"
if grep -q "GITBUTLER_MANAGED_HOOK" "${commit_hook}" 2>/dev/null; then
  commit_hook="${hooks}/pre-commit-user"
fi
printf '#!/bin/sh\n%s\nexec bash "%s"\n' "${marker}" "${proof}" >"${commit_hook}"
chmod +x "${commit_hook}"
echo "installed ${commit_hook}"

push_hook="${hooks}/pre-push"
if [[ ! -f "${push_hook}" ]]; then
  printf '#!/bin/sh\n' >"${push_hook}"
  chmod +x "${push_hook}"
fi
if ! grep -q "${marker}" "${push_hook}"; then
  {
    head -n 1 "${push_hook}"
    printf '%s\nbash "%s" --base origin/main || exit $?\n' "${marker}" "${proof}"
    tail -n +2 "${push_hook}"
  } >"${push_hook}.new"
  chmod +x "${push_hook}.new"
  mv "${push_hook}.new" "${push_hook}"
fi
echo "installed terraform proof in ${push_hook}"
