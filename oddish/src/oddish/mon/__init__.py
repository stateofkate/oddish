"""oddish-mon: the trial driver running inside the sandbox.

Nothing in this package may import ``oddish.db``; it reaches Postgres only
through the control plane's ``/monitor`` routes.
"""
