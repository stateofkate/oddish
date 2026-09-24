#!/usr/bin/env bash
# Remove old Neon branches for closed PRs, after each app's GKE teardown.
# Open PRs keep their data, and this sweep never uses Supabase branch state.
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec python "$script_dir/prune_stale_neon_previews.py"
