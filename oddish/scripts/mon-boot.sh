#!/bin/sh
# Start the monitor in a sandbox booted from a warm snapshot, running the
# launcher's own monitor code. The launcher passes this script's text as the
# start (Modal: the sandbox command; Daytona: the one start exec), so it is
# always the launcher's version. The snapshot keeps what its cache key covers
# (image_cache.snapshot_base_identity: the image, the entrypoint, the locked
# venv, what the warm-up built); its monitor code may be older.
#
# ODDISH_MON_BUNDLE_URL (presigned) and ODDISH_MON_BUNDLE_SHA256 come in the
# sandbox environment, never in the command. They name a tar.gz of the
# launcher's src, scripts/mon-entrypoint.sh, pyproject.toml and uv.lock
# (launcher/code_bundle.py). Fetch it, check its sha256, refuse a lock the
# snapshot's venv was not built from, swap /opt/oddish/src, install the
# entrypoint and start it exactly once. Any failure starts no monitor: the
# snapshot's own code reports the failed start under the attempt's credential,
# and the attempt settles like any other failed start.
set -eu

root=/opt/oddish
entrypoint=/usr/local/bin/mon-entrypoint.sh
log=/var/log/oddish-mon.log

# One line to the provider's log and one to the monitor's log, which the
# failed-start report tails and the trial's artifacts keep.
say() {
  line="$(date -u '+%Y-%m-%d %H:%M:%S') oddish-mon-boot: $*"
  echo "$line"
  echo "$line" >>"$log" 2>/dev/null || :
}

fail() {
  say "metric=monitor_boot outcome=failed"
  say "no monitor started: $*"
  "$root/.venv/bin/python" -m oddish.mon startup-failed 1 || :
  exit 1
}

url=${ODDISH_MON_BUNDLE_URL:-}
sha=${ODDISH_MON_BUNDLE_SHA256:-}
[ -n "$url" ] && [ -n "$sha" ] || fail "the launcher named no code bundle"

# Next to the code it replaces, so the swap is a rename on one filesystem.
work=$(mktemp -d "${root%/*}/oddish-boot.XXXXXX") || fail "no scratch directory"
# curl's own errors name the host at most; any query string is cut anyway.
fetch_s=$(curl -fsS --retry 4 --retry-connrefused --connect-timeout 10 \
  --max-time 60 -o "$work/bundle.tar.gz" -w '%{time_total}' "$url" \
  2>"$work/fetch.err") ||
  fail "code bundle fetch failed: $(tail -n 1 "$work/fetch.err" | sed 's/?[^[:space:]]*//g')"
got=$(sha256sum "$work/bundle.tar.gz" | cut -d ' ' -f 1)
[ "$got" = "$sha" ] || fail "code bundle sha256 is $got, the launcher named $sha"
mkdir "$work/new" || fail "no scratch directory"
tar -xzf "$work/bundle.tar.gz" -C "$work/new" || fail "code bundle does not extract"
[ -d "$work/new/src" ] && [ -f "$work/new/scripts/mon-entrypoint.sh" ] ||
  fail "code bundle has no src or no entrypoint"
# The snapshot's venv was installed from its own lock, and the cache key keeps
# the two equal: a difference is a bug. Never run code against dependencies it
# was not locked with.
for file in uv.lock pyproject.toml; do
  cmp -s "$work/new/$file" "$root/$file" ||
    fail "code bundle's $file differs from the snapshot's"
done

mv "$root/src" "$work/snapshot-src" || fail "could not move the snapshot's code aside"
if ! mv "$work/new/src" "$root/src"; then
  mv "$work/snapshot-src" "$root/src" || :
  fail "could not move the bundle's code in"
fi
install -m 0755 "$work/new/scripts/mon-entrypoint.sh" "$entrypoint" ||
  fail "could not install the bundle's entrypoint"
rm -rf "$work" || :

say "metric=monitor_boot code_source=bundle bundle=$sha fetch_s=$fetch_s"
unset ODDISH_MON_BUNDLE_URL ODDISH_MON_BUNDLE_SHA256
exec "$entrypoint" run
