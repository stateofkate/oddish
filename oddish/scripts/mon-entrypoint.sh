#!/bin/sh
# Start the Docker daemon on the unix socket only, wait for it, then hand over
# to the monitor. No TCP listener: the agent's container must not reach it.
set -eu

command="${1:-run}"

# The control plane hears about a failed start from us, not from a silence
# that lasts until the launcher's deadline: any exit before the monitor has
# reported its result is posted under the attempt's own credential. Best
# effort, and only for a real attempt (a warm-up sandbox has no credential).
report_failed_start() {
  if [ "$command" = run ]; then
    python -m oddish.mon startup-failed "$1" || :
  fi
}

# Whether the control plane already knows how the attempt ended. The monitor's
# status file (oddish/mon/status.py) says so first: "done" once the result was
# accepted, "stopped" once our API answered 410/401, even if the process was
# killed after that. Only without a readable phase does the exit code decide,
# as in oddish/mon/exit_codes.py: 0 = EXIT_ACCEPTED, 3 = EXIT_STOPPED.
attempt_ended() {
  phase=$(sed -n 's/.*"phase": *"\([^"]*\)".*/\1/p' \
    "${ODDISH_MON_STATUS_PATH:-/run/oddish-mon/status.json}" 2>/dev/null) || phase=""
  case "$phase" in
    done | stopped) return 0 ;;
    "") [ "$1" -eq 0 ] || [ "$1" -eq 3 ] ;;
    *) return 1 ;;
  esac
}

# cgroup v2 nesting, as the official docker:dind entrypoint does: move our own
# processes into a child group so the inner daemon can apply CPU and memory
# limits to the task's containers.
if [ -f /sys/fs/cgroup/cgroup.controllers ]; then
  mkdir -p /sys/fs/cgroup/init
  xargs -rn1 < /sys/fs/cgroup/cgroup.procs > /sys/fs/cgroup/init/cgroup.procs 2>/dev/null || :
  sed -e 's/ / +/g' -e 's/^/+/' < /sys/fs/cgroup/cgroup.controllers \
    > /sys/fs/cgroup/cgroup.subtree_control 2>/dev/null || :
fi

# Snapshots preserve files, not process identities. Keep Docker's persistent
# image store, but give each boot fresh containerd/PID/socket runtime state.
runtime_dir=$(mktemp -d /run/oddish-docker.XXXXXX)
dockerd --host=unix:///var/run/docker.sock --exec-root="$runtime_dir" \
  --pidfile="$runtime_dir/docker.pid" >/var/log/dockerd.log 2>&1 &
docker_pid=$!
i=0
until docker info >/dev/null 2>&1; do
  i=$((i + 1))
  if [ "$i" -gt 60 ]; then
    echo "dockerd did not start" >&2
    tail -20 /var/log/dockerd.log >&2
    report_failed_start 1
    exit 1
  fi
  sleep 1
done
if [ "$command" = warm ]; then
  # Finish writes before taking a filesystem snapshot. Unlike pkill + sleep,
  # waiting for our child confirms that Docker has actually shut down.
  status=0
  python -m oddish.mon warm || status=$?
  # dockerd's own exit status (143 on SIGTERM) must not replace the warm result
  # or skip the cleanup under set -e.
  kill "$docker_pid" || :
  wait "$docker_pid" || :
  rm -rf "$runtime_dir"
  exit "$status"
fi
status=0
python -m oddish.mon "$command" || status=$?
if ! attempt_ended "$status"; then
  report_failed_start "$status"
fi
exit "$status"
