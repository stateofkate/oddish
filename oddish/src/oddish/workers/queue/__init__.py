"""The worker-jobs queue. The exports below load on first use, so importing one
submodule (the launcher's ``job_tokens``) does not run ``cleanup``, which
imports the launcher's reaper."""

from importlib import import_module

# Each export and the submodule that defines it.
_EXPORTS = {
    "cleanup_orphaned_queue_state": "cleanup",
    "run_polling_worker": "queue_manager",
    "run_task_expand_job": "task_expand_handler",
    "run_trial_job": "trial_handler",
    "run_worker": "worker",
    "acquire_queue_slot": "slots",
    "cleanup_stale_queue_slots": "slots",
    "ensure_queue_slots": "slots",
    "release_queue_slot": "slots",
    # Unified worker_jobs surface
    "ClaimedWorkerJob": "worker_job_single_job",
    "build_spawn_plan": "worker_job_dispatcher",
    "claim_single_worker_job": "worker_job_single_job",
    "discover_active_worker_job_queue_keys": "worker_job_dispatcher",
    "get_worker_job_org_queue_counts": "worker_job_dispatcher",
    "heartbeat_worker_job": "worker_job_single_job",
    "run_single_worker_job": "worker_job_single_job",
    "select_job_function": "worker_job_dispatcher",
}

__all__ = list(_EXPORTS)


def __getattr__(name: str):
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(import_module(f"{__name__}.{_EXPORTS[name]}"), name)
