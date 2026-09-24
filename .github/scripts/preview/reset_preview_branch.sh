#!/usr/bin/env bash
# Delete a PR's Neon preview branch so the next prepare run clones staging.
# The caller (preview-reset.yml) has already
# verified the PR is an open same-repo PR BEFORE checking out its code.
#
# Deletion is loud (no `|| true`): a reset that cannot delete the branch must
# fail rather than "succeed" against the old, possibly-poisoned database.
set -euo pipefail

: "${PR_NUMBER:?}"
: "${GITHUB_WORKSPACE:?}"

case "$PR_NUMBER" in
  '' | *[!0-9]*)
    echo "PR_NUMBER must be numeric, got '$PR_NUMBER'" >&2
    exit 1
    ;;
esac

python "$GITHUB_WORKSPACE/.github/scripts/neon_database.py" delete-preview
