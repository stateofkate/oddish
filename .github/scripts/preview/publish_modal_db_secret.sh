#!/usr/bin/env bash
# Publish the per-PR database and optional Archil settings as a Modal secret.
set -euo pipefail

: "${MODAL_ENVIRONMENT:?}"
: "${MODAL_APP_NAME:?}"
: "${ODDISH_RUNTIME_DATABASE_URL:?}"

secret_file="$(mktemp)"
trap 'rm -f "$secret_file"' EXIT
python - "$secret_file" <<'PY'
import json
import os
import sys
from pathlib import Path

archil = json.loads(os.environ.get("ARCHIL_PREVIEW_CONFIG") or "{}")
required = {"ARCHIL_API_KEY", "ARCHIL_BASE_URL", "ARCHIL_REGION"}
if not isinstance(archil, dict) or (
    archil
    and (
        archil.keys() != required
        or not all(isinstance(value, str) and value.strip() for value in archil.values())
    )
):
    raise SystemExit(
        "ARCHIL_PREVIEW_CONFIG requires only ARCHIL_API_KEY, ARCHIL_BASE_URL, "
        "and ARCHIL_REGION, all nonempty strings"
    )
Path(sys.argv[1]).write_text(
    json.dumps({"ODDISH_DATABASE_URL": os.environ["ODDISH_RUNTIME_DATABASE_URL"], **archil})
)
PY

uv run modal secret create \
  --env "$MODAL_ENVIRONMENT" --force \
  "$MODAL_APP_NAME-db" \
  --from-json "$secret_file"
