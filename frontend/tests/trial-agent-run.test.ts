import assert from "node:assert/strict";
import test from "node:test";
import {
  trialAgentRun,
  reconcileAgentRunProgress,
  hasAgentRunEnded,
} from "../src/lib/trial-agent-run.ts";
import type { Trial } from "../src/lib/types.ts";

const start = "2026-10-08T12:00:00Z";
const trial = {
  status: "success",
  harbor_stage: "completed",
  total_steps: 960,
  created_at: "2026-10-08T10:00:00Z",
  finished_at: "2026-10-08T14:00:00Z",
  phase_timing: {
    agent_execution: { started_at: start, finished_at: "2026-10-08T12:03:00Z" },
  },
} as Trial;
const now = Date.parse("2026-10-08T12:05:00Z");
test("completed run measures execution, excluding other trial phases", () => {
  assert.deepEqual(trialAgentRun(trial, now), {
    seconds: 180,
    steps: 960,
    running: false,
  });
});
test("only agent_running ticks; verifier boundary stops the clock", () => {
  const active = {
    ...trial,
    status: "running",
    harbor_stage: "agent_running",
    phase_timing: { agent_execution: { started_at: start } },
  } as Trial;
  assert.equal(trialAgentRun(active, now).seconds, 300);
  assert.equal(trialAgentRun(active, now + 1000).seconds, 301);
  for (const harbor_stage of [
    "environment_setup",
    "agent_setup",
    "verification",
    "cleanup",
  ])
    assert.deepEqual(trialAgentRun({ ...active, harbor_stage }, now), {
      seconds: null,
      steps: ["environment_setup", "agent_setup"].includes(harbor_stage)
        ? null
        : 960,
      running: false,
    });
  assert.equal(
    trialAgentRun(
      {
        ...active,
        phase_timing: {
          ...active.phase_timing,
          verifier: {
            started_at: "2026-10-08T12:04:00Z",
            finished_at: "",
            duration_sec: 0,
          },
        },
      },
      now
    ).seconds,
    240
  );
});
test("missing, invalid, failed and stale retry timing never invents zero", () => {
  assert.equal(
    trialAgentRun({ ...trial, phase_timing: null, total_steps: null }, now)
      .seconds,
    null
  );
  assert.equal(
    trialAgentRun({ ...trial, phase_timing: null, total_steps: null }, now)
      .steps,
    null
  );
  assert.equal(
    trialAgentRun(
      {
        ...trial,
        status: "failed",
        phase_timing: { agent_execution: { started_at: start } },
      },
      now
    ).seconds,
    null
  );
  assert.equal(
    trialAgentRun(
      {
        ...trial,
        phase_timing: {
          agent_execution: { started_at: "bad", duration_sec: NaN },
        },
      },
      now
    ).seconds,
    null
  );
  for (const status of ["pending", "queued", "retrying"] as const)
    assert.deepEqual(trialAgentRun({ ...trial, status }, now), {
      seconds: null,
      steps: null,
      running: false,
    });
});
test("recorded duration and genuine zero remain measurable", () => {
  assert.deepEqual(
    trialAgentRun(
      {
        ...trial,
        total_steps: 0,
        phase_timing: { agent_execution: { started_at: "", duration_sec: 0 } },
      },
      now
    ),
    { seconds: 0, steps: 0, running: false }
  );
});

const retry = { ...trial, attempts: 2, started_at: "2026-10-08T12:04:00Z" };
test("retry time requires valid attempt and agent starts, including terminal runs", () => {
  for (const started_at of [null, "", "invalid"])
    for (const status of ["running", "paused", "success", "failed"] as const)
      for (const agentStart of [start, "", "invalid"])
        assert.deepEqual(trialAgentRun({
          ...retry, started_at, status, harbor_stage: "agent_running",
          phase_timing: { agent_execution: { started_at: agentStart, duration_sec: 180 } },
        }, now), { seconds: null, steps: null, running: false });
});

test("invalid list phase starts cannot finish or zero a live interval", () => {
  const detail = { ...trial, id: "run", attempts: 1, status: "running",
    harbor_stage: "agent_running",
    phase_timing: { agent_execution: { started_at: start } },
  } as Trial;
  for (const started_at of ["", "invalid"])
    for (const duration_sec of [0, 180]) {
      const row = { ...detail, phase_timing: {
        agent_execution: { started_at, finished_at: "2026-10-08T12:03:00Z", duration_sec },
        verifier: { started_at, finished_at: "", duration_sec },
      } };
      const merged = reconcileAgentRunProgress(detail, row)!;
      assert.deepEqual(trialAgentRun(merged, now), { seconds: 300, steps: 960, running: true });
      assert.equal(merged.phase_timing?.verifier, undefined);
    }
});

test("new attempt setup, failure and pause never show retained measurements", () => {
  for (const status of ["running", "failed", "paused", "cancelled"] as const) {
    for (const harbor_stage of [
      "starting",
      "trial_started",
      "environment_setup",
      "agent_setup",
      "agent_running",
    ])
      assert.deepEqual(
        trialAgentRun({ ...retry, status, harbor_stage } as Trial, now),
        { seconds: null, steps: null, running: false }
      );
  }
});
test("new agent start ignores older finish/verifier and withholds unscoped retry steps", () => {
  const current = {
    ...retry,
    status: "running",
    harbor_stage: "agent_running",
    phase_timing: {
      agent_execution: {
        started_at: retry.started_at,
        finished_at: "2026-10-08T12:03:00Z",
      },
      verifier: {
        started_at: "2026-10-08T12:03:00Z",
        finished_at: "",
        duration_sec: 0,
      },
    },
  } as Trial;
  assert.deepEqual(trialAgentRun(current, now), {
    seconds: 60,
    steps: null,
    running: true,
  });
  for (const status of ["failed", "paused", "cancelled"] as const)
    assert.equal(
      trialAgentRun({ ...current, status } as Trial, now).seconds,
      null
    );
  assert.deepEqual(
    trialAgentRun(
      {
        ...current,
        status: "success",
        harbor_stage: "completed",
        phase_timing: {
          agent_execution: {
            started_at: retry.started_at,
            finished_at: "2026-10-08T12:05:00Z",
          },
        },
      },
      now
    ),
    { seconds: 60, steps: 960, running: false }
  );
});
test("display uses whole elapsed seconds at minute and hour boundaries", () => {
  for (const [duration, expected] of [
    [59.99, 59],
    [60, 60],
    [119.9, 119],
    [3599.99, 3599],
    [3600, 3600],
  ])
    assert.equal(
      trialAgentRun(
        {
          ...trial,
          phase_timing: {
            agent_execution: { started_at: "", duration_sec: duration },
          },
        },
        now
      ).seconds,
      expected
    );
});

test("card reconciliation takes monotonic list timing while preserving detailed results", () => {
  const detail = {
    ...trial,
    id: "run",
    attempts: 1,
    status: "running",
    harbor_stage: "agent_running",
    phase_timing: { agent_execution: { started_at: start } },
    result: { saved: true },
  } as Trial;
  const row = {
    ...detail,
    result: null,
    total_steps: null,
    harbor_stage: "verification",
    phase_timing: {
      agent_execution: {
        started_at: start,
        finished_at: "2026-10-08T12:03:00Z",
      },
    },
  };
  const merged = reconcileAgentRunProgress(detail, row)!;
  assert.equal(merged.result, detail.result);
  assert.equal(merged.total_steps, 960);
  assert.deepEqual(trialAgentRun(merged, now), {
    seconds: 180,
    steps: 960,
    running: false,
  });
  assert.equal(
    reconcileAgentRunProgress(detail, { ...row, attempts: 0 }),
    detail
  );
  assert.equal(reconcileAgentRunProgress(merged, detail), merged);
  const noTiming = reconcileAgentRunProgress(detail, {
    ...row,
    phase_timing: null,
  })!;
  assert.equal(trialAgentRun(noTiming, now).seconds, null);
  assert.equal(trialAgentRun(noTiming, now).running, false);
  const sameStage = reconcileAgentRunProgress(detail, {
    ...row,
    harbor_stage: "agent_running",
  })!;
  assert.equal(trialAgentRun(sameStage, now).running, false);
});

test("same-attempt fresh phases replace stale detail starts without carrying old ends", () => {
  const detail = {
    ...trial,
    id: "run",
    attempts: 2,
    started_at: "2026-10-08T12:04:00Z",
    status: "running",
    harbor_stage: "agent_running",
    phase_timing: {
      agent_execution: {
        started_at: start,
        finished_at: "2026-10-08T12:03:00Z",
        duration_sec: 180,
      },
      verifier: {
        started_at: "2026-10-08T12:03:00Z",
        finished_at: "2026-10-08T12:03:30Z",
        duration_sec: 30,
      },
    },
  } as Trial;
  const fresh = {
    ...detail,
    phase_timing: { agent_execution: { started_at: detail.started_at! } },
  };
  const active = reconcileAgentRunProgress(detail, fresh)!;
  assert.deepEqual(trialAgentRun(active, now), {
    seconds: 60,
    steps: null,
    running: true,
  });
  const verifying = reconcileAgentRunProgress(active, {
    ...fresh,
    harbor_stage: "verification",
    phase_timing: {
      ...fresh.phase_timing,
      verifier: {
        started_at: "2026-10-08T12:04:30Z",
        finished_at: "",
        duration_sec: 0,
      },
    },
  })!;
  assert.deepEqual(trialAgentRun(verifying, now), {
    seconds: 30,
    steps: null,
    running: false,
  });
});

test("valid incoming finish replaces missing or invalid same-start detail finish", () => {
  for (const finished_at of ["", "invalid", "2026-10-08T11:59:00Z"]) {
    const detail = {
      ...trial,
      id: "run",
      attempts: 1,
      status: "running",
      harbor_stage: "agent_running",
      phase_timing: { agent_execution: { started_at: start, finished_at } },
    } as Trial;
    const row = {
      ...detail,
      phase_timing: {
        agent_execution: {
          started_at: start,
          finished_at: "2026-10-08T12:03:00Z",
        },
      },
    };
    assert.deepEqual(
      trialAgentRun(reconcileAgentRunProgress(detail, row)!, now),
      { seconds: 180, steps: 960, running: false }
    );
  }
});

test("same-attempt pause, retry and cancellation stop stale detail clocks", () => {
  const detail = { ...trial, attempts: 1, status: "running", harbor_stage: "agent_running",
    phase_timing: { agent_execution: { started_at: start } } } as Trial;
  for (const status of ["paused", "retrying", "failed"] as const) {
    const row = { ...detail, status, phase_timing: null };
    const reconciled = reconcileAgentRunProgress(detail, row)!;
    assert.equal(reconciled.status, status);
    assert.equal(trialAgentRun(reconciled, now).running, false);
    assert.equal(reconcileAgentRunProgress(detail, { ...row, attempts: 0 }), detail);
  }
});

test("terminal retries with no current attempt start withhold retained steps", () => {
  for (const status of ["success", "failed"] as const) {
    for (const started_at of [undefined, retry.started_at]) {
      for (const phase_timing of [null, { agent_execution: { started_at: "bad" } }]) {
        assert.equal(trialAgentRun({ ...retry, status, started_at, phase_timing }, now).steps, null);
      }
    }
    assert.equal(trialAgentRun({ ...retry, status, started_at: undefined,
      phase_timing: { agent_execution: { started_at: retry.started_at } } }, now).steps, null);
  }
});

test("cancelled stage stops a running status before terminal detail arrives", () => {
  const detail = { ...trial, attempts: 1, status: "running", harbor_stage: "agent_running",
    phase_timing: { agent_execution: { started_at: start } } } as Trial;
  const row = { ...detail, harbor_stage: "cancelled", phase_timing: null };
  const reconciled = reconcileAgentRunProgress(detail, row)!;
  assert.equal(reconciled.harbor_stage, "cancelled");
  assert.equal(hasAgentRunEnded(reconciled.harbor_stage), true);
  assert.deepEqual(trialAgentRun(reconciled, now), { seconds: null, steps: 960, running: false });
  assert.equal(reconcileAgentRunProgress(reconciled, detail), reconciled);
});
