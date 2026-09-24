#!/usr/bin/env bash
# Point the Vercel preview deployment at this PR's backend and Neon branch.
set -euo pipefail

: "${GITHUB_STEP_SUMMARY:?}"
: "${GITHUB_WORKSPACE:?}"
: "${VERCEL_GIT_BRANCH:?}"
: "${VERCEL_TOKEN:?}"
: "${VERCEL_ORG_ID:?}"
: "${VERCEL_PROJECT_ID:?}"
: "${MODAL_API_URL:?A preview backend URL is required}"
: "${NEON_PROJECT_ID:?A Neon project ID is required}"
: "${NEON_BRANCH_ID:?A preview Neon branch ID is required}"

if [[ ! "$MODAL_API_URL" =~ ^https://[A-Za-z0-9.-]+(:[0-9]+)?/?$ ]]; then
  echo "A valid MODAL_API_URL is required for this PR preview" >&2
  exit 1
fi
if [[ ! "$NEON_PROJECT_ID" =~ ^[a-z0-9]+(-[a-z0-9]+)*$ ]]; then
  echo "A valid NEON_PROJECT_ID is required for this PR preview" >&2
  exit 1
fi
if [[ ! "$NEON_BRANCH_ID" =~ ^br-[a-z0-9]+(-[a-z0-9]+)*$ ]]; then
  echo "A valid NEON_BRANCH_ID is required for this PR preview" >&2
  exit 1
fi

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
github_output="${GITHUB_OUTPUT:-}"
preview_url=""
preview_alias_url=""
backend_api_url="$MODAL_API_URL"
backend_label="${PREVIEW_BACKEND_LABEL:-${MODAL_APP_NAME:-preview Modal backend}}"
database_label="${PREVIEW_DATABASE_LABEL:-Neon ${NEON_BRANCH_ID}}"

# Link to the Neon branch page. These IDs are public metadata, not credentials.
database_url="https://console.neon.tech/app/projects/${NEON_PROJECT_ID}/branches/${NEON_BRANCH_ID}"

read_output_value() {
  local file="$1"
  local key="$2"
  awk -F= -v key="$key" '$1 == key { value = substr($0, length(key) + 2) } END { print value }' "$file"
}

wait_for_url_ready() {
  local url="$1"
  local attempt
  local status

  for attempt in $(seq 1 60); do
    status="$(curl --silent --output /dev/null --write-out '%{http_code}' --max-time 10 "$url" || true)"
    case "$status" in
      2*|3*|401|403)
        return 0
        ;;
    esac
    echo "Waiting for Vercel preview to answer at $url (attempt $attempt/60, status ${status:-none})"
    sleep 5
  done

  echo "Vercel preview never became reachable at $url" >&2
  return 1
}

summarize_vercel_phase() {
  {
    echo "## Vercel preview"
    echo
    if [ -n "$preview_url" ]; then
      echo "- Vercel preview: $preview_url"
      if [ -n "$preview_alias_url" ]; then
        echo "- Stable alias: $preview_alias_url"
      fi
    else
      echo "- Vercel preview: deploy did not produce a URL"
    fi
    echo "- Backend target: $backend_label"
    echo "- Database target: $database_label"
  } >> "$GITHUB_STEP_SUMMARY"
}

trap summarize_vercel_phase EXIT

vercel_output="$(mktemp)"
trap 'rm -f "$vercel_output"; summarize_vercel_phase' EXIT
GITHUB_OUTPUT="$vercel_output" \
  BACKEND_API_URL="$backend_api_url" \
  PREVIEW_BACKEND_LABEL="$backend_label" \
  PREVIEW_DATABASE_LABEL="$database_label" \
  PREVIEW_DATABASE_URL="$database_url" \
  python "$script_dir/redeploy_vercel.py"
[ -z "$github_output" ] || cat "$vercel_output" >> "$github_output"
preview_url="$(read_output_value "$vercel_output" preview_url)"
: "${preview_url:?Vercel did not return a preview URL}"
preview_alias_url="$(read_output_value "$vercel_output" preview_alias_url)"
if [ -n "${PREVIEW_ALIAS_HOSTNAME:-}" ]; then
  [ "$preview_alias_url" = "https://$PREVIEW_ALIAS_HOSTNAME" ] || {
    echo "Vercel did not return the requested preview alias" >&2
    exit 1
  }
  wait_for_url_ready "$preview_alias_url"
else
  wait_for_url_ready "$preview_url"
fi
if [ -n "$github_output" ]; then
  {
    echo "backend_api_url=$backend_api_url"
    echo "backend_label=$backend_label"
    echo "database_label=$database_label"
  } >> "$github_output"
fi
