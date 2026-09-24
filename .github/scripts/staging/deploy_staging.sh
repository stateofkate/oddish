#!/usr/bin/env bash
# Apply pending migrations, then deploy staging on Neon main.
set -euo pipefail

repo_root="${GITHUB_WORKSPACE:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)}"

cd "$repo_root/backend"
uv sync --frozen

: "${ODDISH_DATABASE_URL:?Resolve the Neon main branch before deployment}"
: "${ODDISH_RUNTIME_DATABASE_URL:?Resolve the pooled Neon URL before deployment}"

(
  cd "$repo_root/oddish"
  export UV_PROJECT_ENVIRONMENT="$repo_root/oddish/.venv"
  uv sync --frozen --extra server
  uv run alembic upgrade head
)
uv run alembic upgrade head

# Publish only after both migration chains pass. Existing rows and API keys
# stay in place on every later deploy.
uv run python "$repo_root/.github/scripts/staging/publish_runtime_db.py"
uv run modal deploy deploy.py
