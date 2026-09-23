"""How ``oddish.mon run`` ends. scripts/mon-entrypoint.sh reports a failed start
unless the status file says "done" or "stopped"; only without a readable
status does it fall back to these codes (no report for ACCEPTED or STOPPED)."""

EXIT_ACCEPTED = 0  # the control plane accepted the result
EXIT_FAILED = 1  # the monitor died before reporting: a failed start
EXIT_STOPPED = 3  # the control plane ended the attempt (410/401): report nothing
