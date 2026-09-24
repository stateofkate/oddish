#!/usr/bin/env bash
set -euo pipefail

: "${GITHUB_STEP_SUMMARY:?}"
: "${GITHUB_WORKSPACE:?}"

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
github_output="${GITHUB_OUTPUT:-}"
github_env="${GITHUB_ENV:-}"
branch_ref=""
branch_was_created=""
published_modal_secret=false
schema_upgraded=false

read_output_value() {
  local file="$1"
  local key="$2"
  awk -F= -v key="$key" '$1 == key { value = substr($0, length(key) + 2) } END { print value }' "$file"
}

load_env_file() {
  local file="$1"
  local key value

  while IFS='=' read -r key value; do
    [ -n "$key" ] || continue
    export "$key=$value"
  done < "$file"
}

summarize_database_phase() {
  {
    echo "## Preview database"
    echo
    if [ -n "$branch_ref" ]; then
    echo "- Neon branch: \`$branch_ref\`"
    fi
    echo "- Branch created: \`${branch_was_created:-unknown}\`"
    echo "- Schema upgraded to head: \`$schema_upgraded\`"
    echo "- Existing preview data is kept on redeploy."
    echo "- Modal DB secret published: \`$published_modal_secret\`"
  } >> "$GITHUB_STEP_SUMMARY"
}

trap summarize_database_phase EXIT

neon_env="$(mktemp)"
neon_output="$(mktemp)"
trap 'rm -f "$neon_env" "$neon_output"; summarize_database_phase' EXIT
GITHUB_ENV="$neon_env" GITHUB_OUTPUT="$neon_output" python "$script_dir/../neon_database.py" preview
load_env_file "$neon_env"
[ -z "$github_env" ] || cat "$neon_env" >> "$github_env"
[ -z "$github_output" ] || cat "$neon_output" >> "$github_output"

branch_ref="$(read_output_value "$neon_output" branch_ref)"
branch_was_created="$(read_output_value "$neon_output" branch_was_created)"

# This also completes a first setup interrupted after Neon created the branch.
# Setup has a database marker; later runs only apply migrations and check schema.
# No schema reset, seed refresh, or password change happens during a deploy.
"$script_dir/run_preview_migrations.sh"
schema_upgraded=true

# Keep approval decisions current. The first setup already refreshes counters
# for the trials it stopped; a normal deploy needs no full counter rebuild.
( cd "$GITHUB_WORKSPACE/backend" && uv run python "$script_dir/sync_org_approvals.py" ) &
approval_sync_pid=$!
"$script_dir/publish_modal_db_secret.sh"
published_modal_secret=true
wait "$approval_sync_pid"
