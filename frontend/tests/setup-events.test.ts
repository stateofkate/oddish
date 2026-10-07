import assert from "node:assert/strict";
import test from "node:test";

import {
  foldSetupEvents,
  chronologicalSetupSteps,
  secondarySetupSteps,
  hasSetupInfo,
  isSetupEvent,
  type LiveEventLike,
} from "../src/lib/setup-events.ts";

const T0 = "2026-10-05T23:08:02+00:00";
const T1 = "2026-10-05T23:09:30+00:00";

function subphase(
  seq: number,
  phase: string,
  status: "started" | "success" | "error",
  extra: Record<string, unknown> = {}
): LiveEventLike {
  return {
    seq,
    kind: "env_subphase",
    payload: {
      phase,
      status,
      started_at: T0,
      duration_seconds: null,
      error_type: null,
      ...extra,
    },
  };
}

test("only setup kinds are setup events", () => {
  assert.equal(isSetupEvent(subphase(1, "ec2.ssh_ready", "started")), true);
  assert.equal(
    isSetupEvent({ seq: 2, kind: "env_info", payload: { provider: "ec2" } }),
    true
  );
  assert.equal(isSetupEvent({ seq: 3, kind: "message", payload: {} }), false);
});

test("start then end becomes one finished step with its duration", () => {
  const state = foldSetupEvents([
    subphase(1, "ec2.ssh_ready", "started"),
    subphase(2, "ec2.ssh_ready", "success", { duration_seconds: 22.1 }),
  ]);
  assert.deepEqual(state.steps, [
    {
      phase: "ec2.ssh_ready",
      status: "success",
      startedAt: T0,
      durationSeconds: 22.1,
      errorType: null,
    },
  ]);
  assert.equal(state.current, null);
});

test("the open step is current; a phase that runs twice shows twice", () => {
  const state = foldSetupEvents([
    subphase(1, "kubernetes.ensure_directories", "started"),
    subphase(2, "kubernetes.ensure_directories", "success"),
    subphase(3, "kubernetes.ensure_directories", "started"),
    subphase(4, "kubernetes.ensure_directories", "success"),
    subphase(5, "kubernetes.helm_install", "started", { started_at: T1 }),
  ]);
  assert.deepEqual(
    state.steps.map((step) => [step.phase, step.status]),
    [
      ["kubernetes.ensure_directories", "success"],
      ["kubernetes.ensure_directories", "success"],
      ["kubernetes.helm_install", "started"],
    ]
  );
  assert.equal(state.current?.phase, "kubernetes.helm_install");
  assert.equal(state.current?.startedAt, T1);
});

test("errors keep their type and instance info comes from env_info", () => {
  const state = foldSetupEvents([
    {
      seq: 1,
      kind: "env_info",
      payload: {
        provider: "ec2",
        external_id: "i-0a3f9c1",
        instance_type: "m7i-flex.2xlarge",
        region: "us-west-2",
      },
    },
    subphase(2, "kubernetes.helm_install", "started"),
    subphase(3, "kubernetes.helm_install", "error", {
      error_type: "TimeoutError",
      duration_seconds: 1800,
    }),
    { seq: 4, kind: "message", payload: { text: "ignored" } },
  ]);
  assert.equal(state.instance?.instance_type, "m7i-flex.2xlarge");
  assert.equal(state.steps[0].status, "error");
  assert.equal(state.steps[0].errorType, "TimeoutError");
  assert.equal(state.current, null);
});

test("an instance alone is enough to show setup info", () => {
  const instanceOnly = foldSetupEvents([
    {
      seq: 1,
      kind: "env_info",
      payload: {
        provider: "ec2",
        external_id: "i-0a3f9c1",
        instance_type: null,
        region: null,
      },
    },
  ]);
  assert.equal(instanceOnly.steps.length, 0);
  assert.equal(hasSetupInfo(instanceOnly), true);
  assert.equal(hasSetupInfo(foldSetupEvents([])), false);
});

test("v2 identifies repeated and parallel operations, replay cannot reopen an end", () => {
  const event = (
    seq: number,
    id: string,
    status: string,
    parent: string | null = null
  ) => ({
    ...subphase(seq, "docker.build", "started"),
    payload: {
      ...subphase(seq, "docker.build", "started").payload,
      schema_version: 2,
      operation_id: id,
      parent_operation_id: parent,
      status,
      category: "build",
    },
  });
  const state = foldSetupEvents([
    event(1, "a", "started"),
    event(2, "b", "started", "a"),
    event(3, "a", "success"),
    event(4, "a", "started"),
    event(5, "b", "cancelled", "a"),
  ]);
  assert.equal(state.steps.length, 2);
  assert.equal(state.steps[0].status, "success");
  assert.equal(state.steps[1].status, "cancelled");
  assert.equal(state.steps[1].parentOperationId, "a");
  assert.equal(state.current, null);
});

test("chronological steps use measured start times without changing parent duration", () => {
  const step = (phase: string, startedAt: string, durationSeconds: number) => ({
    phase, startedAt, durationSeconds, status: "success" as const, errorType: null,
  });
  const delivered = [step("late", T1, 3), step("owner", T0, 88), step("child", T0, 5)];
  const ordered = chronologicalSetupSteps(delivered);
  assert.deepEqual(ordered.map((item) => item.phase), ["owner", "child", "late"]);
  assert.equal(ordered[0].durationSeconds, 88);
  assert.equal(delivered[0].phase, "late");
  assert.deepEqual(chronologicalSetupSteps([step("unknown", "", 0), ...delivered]).map((item) => item.phase), ["owner", "child", "late", "unknown"]);
});

test("completed activity snapshots revise wrappers and keep measured child identity", () => {
  const wrapper = { phase: "thunder.create_sandbox", status: "success", started_at: T0, finished_at: T1, duration_seconds: 80, error_type: null, operation_id: "create", parent_operation_id: "root", schema_version: 2 };
  const events: LiveEventLike[] = [
    { seq: 1, kind: "env_subphase", payload: wrapper },
    { seq: 2, kind: "env_subphase", payload: { phase: "environment.setup", status: "success", started_at: T0, finished_at: T1, duration_seconds: 88, error_type: null, operation_id: "root", category: "setup", completed_activities: [
      { ...wrapper, operation_id: "save", phase: "progress.delivery.finished", parent_operation_id: "create", duration_seconds: 8, activity_kind: "supporting" },
      { ...wrapper, duration_seconds: 88 },
    ] } },
  ];
  const state = foldSetupEvents(events);
  assert.equal(state.steps.filter(step => step.operationId === "create").length, 1);
  assert.equal(state.steps.find(step => step.operationId === "create")?.durationSeconds, 88);
  assert.equal(state.steps.find(step => step.operationId === "root")?.hasCompletedActivities, true);
  assert.equal(state.steps.find(step => step.operationId === "save")?.activityKind, "supporting");
  assert.equal(state.steps.find(step => step.operationId === "save")?.parentOperationId, "create");
});

test("secondary setup projection excludes all tertiary activity without changing totals", () => {
  const step = (phase: string, operationId: string, parentOperationId?: string) => ({ phase, operationId, parentOperationId, status: "success" as const, startedAt: T0, durationSeconds: 88, errorType: null });
  const state = { current: null, instance: null, steps: [
    { ...step("environment.setup", "root"), category: "setup" as const },
    step("thunder.create_sandbox", "create", "root"),
    step("thunder.create_sandbox.operation", "body", "create"),
    step("thunder.resolve_image", "nested", "body"),
    step("progress.delivery.started", "save", "root"),
    step("progress.delivery.finished", "save-child", "create"),
    step("legacy.build", "legacy"),
  ] };
  assert.deepEqual(secondarySetupSteps(state).map(item => item.phase), ["thunder.create_sandbox", "legacy.build"]);
  assert.equal(secondarySetupSteps(state)[0].durationSeconds, 88);
  assert.equal(state.steps.length, 7);
});
