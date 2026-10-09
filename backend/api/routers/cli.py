"""Install the Oddish CLI with an Oddish API key instead of GitHub access.

Releases live in the private ``abundant-ai/homebrew-tap`` repository. The
backend reads them with its own GitHub token and hands a signed-in caller the
newest client archive's version, SHA-256, and a short-lived signed download
URL (GitHub's own redirect target), so no GitHub credential leaves the server.
"""

from __future__ import annotations

import os
import time
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from auth import AuthContext, require_signed_in

router = APIRouter(prefix="/cli", tags=["CLI"])

TAP_REPO = "abundant-ai/homebrew-tap"
_GITHUB = "https://api.github.com"
_CACHE_SECONDS = 300
_cache: dict[str, object] = {}


class CliRelease(BaseModel):
    version: str
    sha256: str
    url: str


def _token() -> str:
    token = os.getenv("ODDISH_CLI_RELEASE_TOKEN") or os.getenv("GITHUB_TOKEN")
    if not token:
        raise HTTPException(503, "CLI downloads are not configured")
    return token


def _headers(token: str, *, binary: bool = False) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/octet-stream"
        if binary
        else "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


async def _latest(client: httpx.AsyncClient, token: str) -> tuple[str, str, str]:
    """(version, sha256, archive asset API URL) of the newest release."""
    cached = _cache.get("latest")
    if cached and time.monotonic() - _cache["at"] < _CACHE_SECONDS:  # type: ignore[operator]
        return cached  # type: ignore[return-value]
    release = await client.get(
        f"{_GITHUB}/repos/{TAP_REPO}/releases/latest", headers=_headers(token)
    )
    if release.status_code != 200:
        raise HTTPException(503, "CLI release lookup failed")
    body = release.json()
    version = str(body["tag_name"]).removeprefix("v")
    assets = {asset["name"]: asset["url"] for asset in body.get("assets", [])}
    archive_name = f"oddish-{version}.tar.gz"
    if archive_name not in assets or "SHA256SUMS" not in assets:
        raise HTTPException(503, "CLI release is missing its archive or checksums")
    sums = await client.get(
        assets["SHA256SUMS"],
        headers=_headers(token, binary=True),
        follow_redirects=True,
    )
    sha256 = next(
        (
            line.split()[0]
            for line in sums.text.splitlines()
            if line.split()[-1:] == [archive_name]
        ),
        None,
    )
    if sums.status_code != 200 or not sha256:
        raise HTTPException(503, "CLI release checksum lookup failed")
    result = (version, sha256, assets[archive_name])
    _cache.update(latest=result, at=time.monotonic())
    return result


@router.get("/latest", response_model=CliRelease)
async def latest_cli(
    auth: Annotated[AuthContext, Depends(require_signed_in)],
    format: str = Query("json", pattern="^(json|text)$"),
):
    """The newest CLI archive for any signed-in org, view-only included.

    ``format=text`` answers ``version sha256 url`` on one line for install.sh.
    """
    token = _token()
    async with httpx.AsyncClient(timeout=20) as client:
        version, sha256, asset_url = await _latest(client, token)
        # GitHub answers an authenticated asset request with a redirect to a
        # signed URL that expires in minutes; that URL is what we hand out.
        signed = await client.get(
            asset_url, headers=_headers(token, binary=True), follow_redirects=False
        )
    url = signed.headers.get("location")
    if signed.status_code not in (301, 302, 307) or not url:
        raise HTTPException(503, "CLI download link failed")
    if format == "text":
        return PlainTextResponse(f"{version} {sha256} {url}\n")
    return CliRelease(version=version, sha256=sha256, url=url)


@router.get("/install.sh", response_class=PlainTextResponse)
async def install_script() -> PlainTextResponse:
    """Public script; it downloads nothing without a valid ODDISH_API_KEY."""
    return PlainTextResponse(INSTALL_SH, media_type="text/x-shellscript")


INSTALL_SH = r"""#!/bin/sh
# Install the Oddish CLI with your Oddish API key.
#   export ODDISH_API_KEY=ok_...
#   curl -fsSL https://abundant-ai--api.modal.run/cli/install.sh | sh
# Re-run it to update. Installs to ~/.oddish/cli and links ~/.local/bin/oddish.
set -eu

API="${ODDISH_API_URL:-https://abundant-ai--api.modal.run}"
[ -n "${ODDISH_API_KEY:-}" ] || { echo "Set ODDISH_API_KEY first (Oddish Settings > API keys)." >&2; exit 1; }

case "$(uname -s)/$(uname -m)" in
  Darwin/arm64|Linux/x86_64) ;;
  *) echo "Supported: Apple Silicon macOS and Linux x86_64." >&2; exit 1 ;;
esac

if ! command -v uv >/dev/null 2>&1; then
  echo "Installing uv (Python package manager)..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  PATH="$HOME/.local/bin:$PATH"
fi

release=$(curl -fsS -H "Authorization: Bearer $ODDISH_API_KEY" "$API/cli/latest?format=text") || {
  echo "Could not get the CLI release. Check ODDISH_API_KEY." >&2; exit 1; }
version=$(echo "$release" | cut -d' ' -f1)
sha256=$(echo "$release" | cut -d' ' -f2)
url=$(echo "$release" | cut -d' ' -f3)

root="$HOME/.oddish/cli"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
curl -fsSL "$url" -o "$tmp/oddish.tar.gz"
if command -v sha256sum >/dev/null 2>&1; then
  got=$(sha256sum "$tmp/oddish.tar.gz" | cut -d' ' -f1)
else
  got=$(shasum -a 256 "$tmp/oddish.tar.gz" | cut -d' ' -f1)
fi
[ "$got" = "$sha256" ] || { echo "Checksum mismatch; not installing." >&2; exit 1; }

mkdir -p "$root"
rm -rf "$root/oddish-$version"
tar xzf "$tmp/oddish.tar.gz" -C "$root"
# The archive's frozen lockfile pins every dependency; nothing resolves fresh.
UV_PROJECT_ENVIRONMENT="$root/venv" uv sync --project "$root/oddish-$version" \
  --frozen --no-dev --no-editable --python 3.13 --quiet
# Mark the install so `oddish update` says to re-run this script (Homebrew
# installs carry a HOMEBREW marker the same way).
for dist in "$root"/venv/lib/python*/site-packages/oddish-*.dist-info; do
  printf 'script\n' > "$dist/ODDISH_INSTALL"
done
mkdir -p "$HOME/.local/bin"
ln -sf "$root/venv/bin/oddish" "$HOME/.local/bin/oddish"

echo "Installed oddish $version."
case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) echo "Add ~/.local/bin to your PATH to run oddish." ;;
esac
"""
