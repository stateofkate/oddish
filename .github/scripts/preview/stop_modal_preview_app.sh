#!/usr/bin/env bash
# Stop a PR app before an explicit database reset. Normal deploys do not use
# this script. A failed stop must block the next step, which deletes the DB.
set -euo pipefail

: "${MODAL_ENVIRONMENT:?}"
: "${MODAL_APP_NAME:?}"

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

read_app_state() {
  local listing
  listing="$(modal app list --env "$MODAL_ENVIRONMENT" --json)" || return 1
  # Modal versions use either lowercase JSON keys or the display column names.
  jq -er --arg name "$MODAL_APP_NAME" '
    if type != "array" then error("invalid app list")
    else map(
      if type != "object" then error("invalid app entry")
      else {
        description: (if has("description") then .description else .Description end),
        state: (if has("state") then .state else .State end)
      } end
    )
      | if any(.[]; (.description | type) != "string" or (.state | type) != "string")
        then error("invalid app entry") else . end
      | [.[] | select(.description == $name) | .state]
      | if length == 0 then "absent"
        elif any(.[]; . != "stopped" and . != "deployed" and . != "stopping...") then error("unknown app state")
        elif any(.[]; . == "deployed" or . == "stopping...") then "deployed"
        else "stopped"
        end
    end' <<<"$listing"
}

if ! app_state="$(read_app_state)"; then
  echo "::error::Cannot check $MODAL_APP_NAME. Database reset is blocked." >&2
  exit 1
fi
if [ "$app_state" = "absent" ] || [ "$app_state" = "stopped" ]; then
  echo "$MODAL_APP_NAME is $app_state; no app needs to stop."
  exit 0
fi

# The running app holds its GKE credentials. Keep it and its reaper alive
# if cluster teardown fails; the reset must leave its database in place too.
if ! "$script_dir/run_gke_teardown.sh" "$MODAL_APP_NAME"; then
  echo "::error::GKE teardown failed; leaving $MODAL_APP_NAME running. Database reset is blocked." >&2
  exit 1
fi

if ! modal app stop -y --env "$MODAL_ENVIRONMENT" "$MODAL_APP_NAME"; then
  echo "::error::Could not stop $MODAL_APP_NAME. Database reset is blocked." >&2
  exit 1
fi

# Allow a short delay for the stopped state to reach Modal's app list.
for attempt in {1..10}; do
  if ! app_state="$(read_app_state)"; then
    echo "::error::Cannot confirm that $MODAL_APP_NAME stopped. Database reset is blocked." >&2
    exit 1
  fi
  if [ "$app_state" = "absent" ] || [ "$app_state" = "stopped" ]; then
    echo "$MODAL_APP_NAME stopped; database reset can continue."
    exit 0
  fi
  [ "$attempt" -eq 10 ] || sleep 2
done
echo "::error::$MODAL_APP_NAME is still running. Database reset is blocked." >&2
exit 1
