/**
 * Environment setup progress from the trial's live events.
 *
 * The worker writes `env_subphase` rows (one per Harbor setup step start and
 * end) and one `env_info` row (where the trial runs) before the agent starts.
 * See oddish/src/oddish/workers/harbor/setup_events.py for the payloads.
 */

export type SetupStepStatus = "started" | "success" | "error" | "cancelled";

export interface LiveEventLike {
  seq: number;
  kind: string;
  payload: Record<string, unknown>;
}

// Payloads are type aliases, not interfaces, so they stay assignable to the
// generic live-event payload (Record<string, unknown>).
export type EnvironmentActivity = {
  phase: string;
  status: SetupStepStatus;
  started_at: string;
  duration_seconds: number | null;
  error_type: string | null;
  schema_version?: 1 | 2;
  operation_id?: string | null;
  parent_operation_id?: string | null;
  category?:
    | "setup"
    | "resolve"
    | "provision"
    | "stage"
    | "build"
    | "start"
    | "ready"
    | "finalize"
    | "cleanup"
    | "other";
  provider?: string | null;
  resource_layer?: "task_environment";
  strategy?: string | null;
  operation_attempt?: number;
  finished_at?: string | null;
  activity_kind?: "operation" | "supporting";
};

export type CompletedEnvironmentActivity = EnvironmentActivity & {
  schema_version: 2;
  operation_id: string;
  finished_at: string;
  duration_seconds: number;
  status: Exclude<SetupStepStatus, "started">;
};

export type EnvSubphasePayload = EnvironmentActivity & {
  completed_activities?: CompletedEnvironmentActivity[] | null;
};

export type EnvInfo = {
  provider: string;
  external_id: string | null;
  instance_type: string | null;
  region: string | null;
};

export interface EnvSubphaseEvent {
  seq: number;
  kind: "env_subphase";
  payload: EnvSubphasePayload;
}

export interface EnvInfoEvent {
  seq: number;
  kind: "env_info";
  payload: EnvInfo;
}

export type SetupEvent = EnvSubphaseEvent | EnvInfoEvent;

export function isSetupEvent(event: LiveEventLike): event is SetupEvent {
  return event.kind === "env_subphase" || event.kind === "env_info";
}

export interface SetupStep {
  /** Raw Harbor phase name, e.g. "kubernetes.helm_install". */
  phase: string;
  status: SetupStepStatus;
  startedAt: string;
  durationSeconds: number | null;
  errorType: string | null;
  operationId?: string | null;
  parentOperationId?: string | null;
  category?: EnvSubphasePayload["category"];
  finishedAt?: string | null;
  provider?: string | null;
  strategy?: string | null;
  operationAttempt?: number;
  activityKind?: "operation" | "supporting";
  hasCompletedActivities?: boolean;
}

export interface SetupState {
  steps: SetupStep[];
  /** The step still running, if any. */
  current: SetupStep | null;
  instance: EnvInfo | null;
}

/** Anything to show: steps, or just the instance before the first step. */
export function hasSetupInfo(state: SetupState): boolean {
  return state.steps.length > 0 || state.instance !== null;
}

export const EMPTY_SETUP: SetupState = {
  steps: [],
  current: null,
  instance: null,
};

/**
 * Fold setup events (in seq order) into steps. A start opens a step; an end
 * closes the most recent open step with the same phase, so a phase that runs
 * twice shows twice.
 */
export function foldSetupEvents(events: readonly LiveEventLike[]): SetupState {
  const steps: SetupStep[] = [];
  let instance: EnvInfo | null = null;
  const expanded = events.flatMap((event): LiveEventLike[] => {
    if (!isSetupEvent(event) || event.kind !== "env_subphase") return [event];
    return [event, ...(event.payload.completed_activities ?? []).map((payload) => ({
      seq: event.seq, kind: "env_subphase", payload,
    }))];
  });
  for (const event of expanded) {
    if (!isSetupEvent(event)) continue;
    if (event.kind === "env_info") {
      instance = event.payload;
      continue;
    }
    const { phase, status, started_at, duration_seconds, error_type } =
      event.payload;
    const detail = {
      operationId: event.payload.operation_id,
      parentOperationId: event.payload.parent_operation_id,
      category: event.payload.category,
      finishedAt: event.payload.finished_at,
      provider: event.payload.provider,
      strategy: event.payload.strategy,
      operationAttempt: event.payload.operation_attempt,
      activityKind: event.payload.activity_kind,
      hasCompletedActivities: event.payload.completed_activities != null,
    };
    if (detail.operationId) {
      const index = steps.findIndex(
        (step) => step.operationId === detail.operationId
      );
      // Replayed starts must never reopen a finished operation.
      if (
        index >= 0 &&
        status === "started" &&
        steps[index].status !== "started"
      )
        continue;
      const step = {
        phase,
        status,
        startedAt: started_at,
        durationSeconds: duration_seconds,
        errorType: error_type,
        ...detail,
      };
      if (index < 0) steps.push(step);
      else steps[index] = { ...step, hasCompletedActivities: step.hasCompletedActivities || steps[index].hasCompletedActivities };
      continue;
    }
    if (status === "started") {
      steps.push({
        phase,
        status,
        startedAt: started_at,
        durationSeconds: null,
        errorType: null,
      });
      continue;
    }
    let index = -1;
    for (let i = steps.length - 1; i >= 0; i--) {
      if (steps[i].phase === phase && steps[i].status === "started") {
        index = i;
        break;
      }
    }
    const ended: SetupStep = {
      phase,
      status,
      startedAt: started_at,
      durationSeconds: duration_seconds,
      errorType: error_type,
    };
    if (index >= 0) steps[index] = ended;
    else steps.push(ended);
  }
  let current: SetupStep | null = null;
  for (let i = steps.length - 1; i >= 0; i--) {
    if (steps[i].status === "started") {
      current = steps[i];
      break;
    }
  }
  return { steps, current, instance };
}

export function setupStepLabel(step: SetupStep): string {
  if (step.phase.startsWith("progress.delivery.")) return "Save progress";
  if (step.phase === "environment.record_sandbox") return "Record sandbox details";
  const body = step.phase.endsWith(".operation");
  const phase = body ? step.phase.slice(0, -".operation".length) : step.phase;
  const operation = phase.split(".").at(-1) ?? phase;
  const labels: Record<string, string> = {
    docker_ready: "Docker ready",
    start_docker_daemon: "Start Docker service",
    install_egress_ca: "Install network certificates",
    compose_build: "Build environment",
    compose_up: "Start services",
    helm_install: "Install environment",
    main_pod_ready: "Wait for pod readiness",
    setup: "Environment setup",
    resolve_image: "Select image",
    create_sandbox: "Create sandbox",
    wait_ready: "Wait for sandbox",
    sandbox_ready: "Wait for sandbox",
    finalize: "Finish setup",
    record_sandbox: "Record sandbox details",
    stage_files: "Upload files",
    prepare_dirs: "Finish setup",
  };
  const bodyLabels: Record<string, string> = {
    resolve_image: "Resolve image",
    create_sandbox: "Provision sandbox",
    wait_ready: "Check sandbox readiness",
    sandbox_ready: "Check sandbox readiness",
    finalize: "Prepare setup files and directories",
    record_sandbox: "Save sandbox identity",
    docker_ready: "Start Docker",
    start_docker_daemon: "Start Docker daemon",
    install_egress_ca: "Install certificate trust",
    stage_files: "Transfer setup files",
    compose_build: "Build container image",
    compose_up: "Start containers",
    prepare_dirs: "Prepare working directories",
  };
  return (
    (body ? bodyLabels[operation] : undefined) ?? labels[operation] ??
    operation.replaceAll("_", " ").replace(/^./, (c) => c.toUpperCase())
  );
}

/** Start-time order is independent of delivery order; ties retain source order. */
export function chronologicalSetupSteps(steps: readonly SetupStep[]): SetupStep[] {
  return [...steps].sort((a, b) => {
    const left = Date.parse(a.startedAt);
    const right = Date.parse(b.startedAt);
    if (!Number.isFinite(left)) return Number.isFinite(right) ? 1 : 0;
    if (!Number.isFinite(right)) return -1;
    return left - right;
  });
}

/** Only measured secondary operations belong in the customer-facing setup list. */
export function secondarySetupSteps(state: SetupState): SetupStep[] {
  const roots = new Set(state.steps.filter(step => step.category === "setup" && !step.parentOperationId)
    .map(step => step.operationId).filter(Boolean));
  return chronologicalSetupSteps(state.steps.filter(step =>
    step.category !== "setup" && !step.phase.endsWith(".operation") &&
    !step.phase.startsWith("progress.delivery.") &&
    (!step.parentOperationId || roots.has(step.parentOperationId))
  ));
}
