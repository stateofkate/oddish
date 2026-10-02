import type { Trajectory, TrajectoryStep } from "@/lib/types";

// Recorded shapes from terminus-2, claude-code and codex trajectories.
const step = (id: number, data: Partial<TrajectoryStep>): TrajectoryStep => ({
  step_id: id,
  timestamp: `2026-09-22T18:00:${String(id).padStart(2, "0")}Z`,
  source: "agent",
  model_name: "example-model",
  message: "",
  reasoning_content: null,
  tool_calls: null,
  observation: null,
  metrics: null,
  ...data,
});

const text = (value: string) => ({ type: "text" as const, text: value });
const process = (output: string, exit_code: number | null, wall = 0.4) =>
  JSON.stringify({
    output,
    wall_time_seconds: wall,
    ...(exit_code === null ? { session_id: 7 } : { exit_code }),
    chunk_id: "a1b2c3",
  });

export const longCommand =
  "kubectl describe pod -n controlplane-canary kcm-mount-canary-cp-ridgeline-eu-1 2>&1";
export const deepJson = JSON.stringify({
  model: { layers: { attention: { heads: { count: 8, dim: 64 } } } },
});
export const trainingLog = Array.from(
  { length: 60 },
  (_, i) =>
    `\x1b[32mINFO\x1b[0m step ${i + 1}/60 loss=${(2 - i / 40).toFixed(3)}`
).join("\n");
const script = `const [status, metrics] = await Promise.allSettled([
  tools.exec_command({ cmd: "git status --short" }),
  tools.exec_command({ cmd: "cat metrics.json" }),
]);
text(JSON.stringify([status, metrics].map((r, i) => ({ i, ...r }))));
text(await tools.exec_command({ cmd: "python train.py --epochs 1" }));`;
const patch = `*** Begin Patch
*** Update File: src/rules.py
@@
-    return match(rule, line)
+    return rule.enabled and match(rule, line)
*** Add File: tests/test_rules.py
+def test_disabled_rule_skips():
+    assert not apply(Rule(enabled=False), "x")
*** End Patch
`;
export const parseDiagnostic =
  "Previous response had parsing errors:\nERROR: Invalid JSON: Invalid control character at: line 3 column 1 (char 57) | Content preview: '{...'.\nPlease fix these issues and provide a proper JSON response.";

export const trajectory: Trajectory = {
  schema_version: "1.0",
  session_id: "trajectory-fixture",
  agent: { name: "codex", version: "fixture", model_name: "example-model" },
  notes: null,
  final_metrics: null,
  steps: [
    step(1, {
      source: "user",
      model_name: null,
      message:
        "<environment_context>\n  <cwd>/app</cwd>\n  <approval_policy>never</approval_policy>\n  <shell>bash</shell>\n</environment_context>",
    }),
    step(2, {
      source: "user",
      model_name: null,
      message:
        "## Task\n\nMake disabled rules **skip** matching.\n\n- Keep the public `apply` API.\n- Add a test.\n\n```bash\npytest tests/\n```",
    }),
    step(3, {
      message: "I'll look at the repository state first.",
      reasoning_content:
        "The rule engine lives in `src/rules.py`; check the test layout before editing.\n\nConfirm the exact rule and its tests before writing a change.",
      tool_calls: [
        {
          tool_call_id: "status",
          function_name: "bash_command",
          arguments: {
            keystrokes: "git status && git branch\n",
            duration: 0.5,
          },
        },
        {
          tool_call_id: "read",
          function_name: "bash_command",
          arguments: { keystrokes: "head -n 5 src/rules.py\n", duration: 0.5 },
        },
      ],
      observation: {
        results: [
          {
            source_call_id: "status",
            content: "On branch main\nnothing to commit, working tree clean",
          },
          {
            source_call_id: "read",
            content: "def apply(rule, line):\n    return match(rule, line)",
          },
          { source_call_id: null, content: "root@sandbox:/app# " },
        ],
      },
    }),
    step(4, {
      message:
        '{\n  "commands": [{"keystrokes": "python3 -c "\nprint(1)""}]\n}',
      reasoning_content: "The response format needs a valid JSON string.",
      observation: {
        results: [{ source_call_id: null, content: parseDiagnostic }],
      },
    }),
    step(5, {
      model_name: "claude-haiku-4-5-20251001",
      message: `Executed command: ${longCommand}`,
      tool_calls: [
        {
          tool_call_id: "long",
          function_name: "Bash",
          arguments: {
            command: longCommand,
            description: "Describe the canary pod",
          },
        },
      ],
      observation: {
        results: [
          {
            source_call_id: "long",
            content: "Error from server (NotFound): pods not found",
          },
        ],
      },
    }),
    step(6, {
      tool_calls: [
        {
          tool_call_id: "exec",
          function_name: "exec",
          arguments: { input: script },
        },
      ],
      observation: {
        results: [
          {
            source_call_id: "exec",
            content: [
              text("Script completed\nWall time 41.2 seconds\nOutput:\n"),
              text(
                JSON.stringify([
                  {
                    i: 0,
                    status: "fulfilled",
                    value: JSON.parse(process(" M src/rules.py\n", 0, 0.1)),
                  },
                  {
                    i: 1,
                    status: "fulfilled",
                    value: JSON.parse(process(deepJson, 0, 0.1)),
                  },
                ])
              ),
              text(process(trainingLog, 1, 41)),
            ],
          },
        ],
      },
    }),
    step(7, {
      tool_calls: [
        {
          tool_call_id: "patch",
          function_name: "apply_patch",
          arguments: { input: patch },
        },
      ],
      observation: {
        results: [
          {
            source_call_id: "patch",
            content:
              "Success. Updated the following files:\nM src/rules.py\nA tests/test_rules.py",
          },
        ],
      },
    }),
    step(8, {
      tool_calls: [
        {
          tool_call_id: "poll",
          function_name: "exec",
          arguments: {
            input:
              'text(await tools.write_stdin({ session_id: 7, chars: "" }));',
          },
        },
      ],
      observation: {
        results: [
          { source_call_id: "poll", content: [text(process("", null, 5))] },
        ],
      },
    }),
    step(9, {
      tool_calls: [
        {
          tool_call_id: "custom",
          function_name: "update_plan",
          arguments: {
            plan: [
              { step: "Fix rule", status: "completed" },
              { step: "Add test", status: "in_progress" },
            ],
          },
        },
      ],
      observation: {
        results: [{ source_call_id: "custom", content: "Plan updated" }],
      },
    }),
    step(10, { message: '{\n  "answer": 42,\n  "files": ["src/rules.py"]\n}' }),
    step(11, {
      message:
        "Disabled rules now return early, and `tests/test_rules.py` covers it. All tests pass.",
    }),
    step(12, {
      message: "Reasoning",
      reasoning_content:
        "A disabled rule should stop before matching.\n\nThe public function can keep the same inputs and return type.",
    }),
    step(13, {
      message:
        "I will keep the function signature as it is.\n\nThe change belongs inside the existing rule check.",
    }),
    step(14, {
      message: "The test must cover a disabled rule that would otherwise match.",
      reasoning_content:
        "The test must cover a disabled rule that would otherwise match.",
    }),
  ],
};

// Live transcript events as the Claude live tail records them: tool input
// and results arrive as JSON text.
export const liveEvents = [
  {
    kind: "message",
    payload: {
      turn_id: "t1",
      block_index: 0,
      text: "Running the **test suite** now.",
    },
  },
  {
    kind: "tool_use",
    payload: {
      turn_id: "t1",
      name: "Bash",
      input: JSON.stringify({
        command: "pytest -q tests/",
        description: "Run tests",
      }),
    },
  },
  {
    kind: "tool_result",
    payload: { turn_id: "t1", content: "2 passed in 0.31s" },
  },
  {
    kind: "tool_use",
    payload: { turn_id: "t2", name: "Bash", input: '{"command": "cat metr' },
  },
].map((event, index) => ({ seq: index + 1, ...event }));
