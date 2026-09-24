import type { Task } from "@/lib/types";

/**
 * Return the version represented by an experiment task's already-scoped trials.
 * Fall back to the selected task default when the experiment has no versioned
 * trial rows to anchor its file and reward views.
 */
export function resolveExperimentTaskVersion(task: Task): number | null {
  if (task.trial_version !== undefined) return task.trial_version;
  const representedVersion = task.trials?.find(
    (trial) => trial.task_version != null,
  )?.task_version;
  return representedVersion ?? task.current_version ?? null;
}

/**
 * The task's default version when an experiment's trials ran a different one.
 * QA and pre-trial audits act on the default version, and the task's verdict
 * fields describe it, so an experiment on another version must not offer them.
 * The task page also sets trial_version (to the picked version), so only a task
 * shown inside an experiment counts.
 */
export function defaultVersionElsewhere(task: Task): number | null {
  if (!task.experiment_id) return null;
  const shown = task.trial_version;
  const defaultVersion = task.current_version;
  return shown != null && defaultVersion != null && shown !== defaultVersion
    ? defaultVersion
    : null;
}
