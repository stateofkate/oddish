"""Print the current staging runtime URL from a Modal secret for recovery.

Run from backend/:

    uv run modal run --env staging ../.github/scripts/staging/emit_staging_db_url.py

Modal writes progress output around the URL. Select the line that starts with
``postgresql`` and check it is non-empty before using it. Set STAGING_DB_SECRET
to inspect another staging secret. If the local data move saved the old source
URL as oddish-staging-supabase-source, read it with:

    STAGING_DB_SECRET=oddish-staging-supabase-source uv run modal run --env staging ../.github/scripts/staging/emit_staging_db_url.py

Staging deployments now resolve the direct and pooled URLs from the Neon API.
They publish the pooled URL to oddish-staging-db after migrations pass. The
one-time Supabase data move runs outside this repository and GitHub Actions.
"""

import os

import modal

SECRET_NAME = os.environ.get("STAGING_DB_SECRET", "oddish-staging-db")

app = modal.App("emit-staging-db-url")
image = modal.Image.debian_slim()


@app.function(
    image=image,
    secrets=[modal.Secret.from_name(SECRET_NAME, environment_name="staging")],
)
def emit() -> str:
    import os

    return os.environ["ODDISH_DATABASE_URL"]


@app.local_entrypoint()
def main() -> None:
    print(emit.remote())
