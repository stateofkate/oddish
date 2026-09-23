import assert from "node:assert/strict";
import test from "node:test";
import {
  outputViews,
  stepTitle,
  stepView,
  taggedContext,
  toolInput,
  toolTitle,
} from "../src/lib/trajectory-view.ts";
import type { TrajectoryStep } from "../src/lib/types.ts";

const step = (data: Partial<TrajectoryStep>): TrajectoryStep => ({
  step_id: 1,
  timestamp: null,
  source: "agent",
  model_name: null,
  message: "",
  reasoning_content: null,
  tool_calls: null,
  observation: null,
  metrics: null,
  ...data,
});

const codes = (content: Parameters<typeof outputViews>[0]) =>
  outputViews(content).outputs.map((view) =>
    view.kind === "code" ? [view.code, view.language, view.meta] : view.kind
  );

test("each producer's shell call shows its command, other scalars as meta", () => {
  assert.deepEqual(
    toolInput("bash_command", { keystrokes: "ls\n", duration: 0.5 }),
    {
      code: "ls",
      language: "bash",
      meta: ["duration 0.5"],
    }
  );
  assert.equal(
    toolInput("Bash", { command: "pwd", description: "Where" })?.code,
    "pwd"
  );
  assert.equal(
    toolInput("exec_command", { cmd: "git status" })?.code,
    "git status"
  );
  assert.equal(
    toolInput("shell", { command: ["bash", "-lc", "make test"] })?.code,
    "make test"
  );
  assert.equal(
    toolTitle("run_shell_command", { command: "npm test\nnpm run lint" }),
    "npm test"
  );
});

test("unknown tools and non-scalar extras keep their JSON arguments", () => {
  const custom = toolInput("custom_tool", {
    command: "data",
    nested: { on: true },
  });
  assert.equal(custom?.language, "json");
  assert.deepEqual(JSON.parse(custom!.code), {
    command: "data",
    nested: { on: true },
  });
  assert.equal(
    toolInput("Bash", { command: "ls", env: { A: "1" } })?.language,
    "json"
  );
  assert.equal(toolInput("view_image", {}), null);
  assert.equal(toolTitle("view_image", { path: "a.png" }), "view_image");
});

test("patches and code-mode scripts keep their source; titles summarize them", () => {
  const patch =
    "*** Begin Patch\n*** Add File: /app/out.txt\n+done\n*** Update File: src/a.py\n@@\n-x\n+y\n*** End Patch\n";
  assert.equal(toolInput("apply_patch", { input: patch })?.language, "diff");
  assert.equal(
    toolTitle("apply_patch", { input: patch }),
    "Edit out.txt, a.py"
  );

  const script =
    "const [a, b] = await Promise.allSettled([tools.exec_command({cmd: \"pytest -x\"}), tools.exec_command({cmd: 'ls'})]);";
  assert.equal(toolInput("exec", { input: script })?.code, script);
  assert.equal(toolTitle("exec", { input: script }), "pytest -x (+1 more)");
  assert.equal(
    toolTitle("exec", {
      input: "text(await tools.write_stdin({session_id: 4}))",
    }),
    "write_stdin"
  );
});

test("codex exec output unwraps blocks, settled promises and process records", () => {
  const settled = [
    {
      i: 0,
      status: "fulfilled",
      value: { output: "ok\n", exit_code: 0, wall_time_seconds: 0.25 },
    },
    { i: 1, status: "rejected", reason: "timed out" },
  ];
  const content = [
    {
      type: "text" as const,
      text: "Script completed\nWall time 1.5 seconds\nOutput:\n",
    },
    { type: "text" as const, text: JSON.stringify(settled) },
    {
      type: "text" as const,
      text: JSON.stringify({
        output: "",
        session_id: 42,
        wall_time_seconds: 1,
      }),
    },
  ];
  assert.deepEqual(outputViews(content).meta, ["script 1.5s"]);
  assert.deepEqual(codes(content), [
    ["ok\n", "text", ["call 1", "exit 0", "0.3s"]],
    ["timed out", "text", ["call 2", "rejected"]],
    ["", "text", ["session 42 running", "1.0s"]],
  ]);
});

test("JSON is indented at any depth and Copy keeps the recorded text", () => {
  const deep = JSON.stringify({
    model: { layers: { attn: { heads: { n: 8 } } } },
  });
  const [printed] = outputViews(
    JSON.stringify({ output: deep, exit_code: 0, wall_time_seconds: 0 })
  ).outputs;
  assert.equal(printed.kind === "code" && printed.language, "json");
  assert.equal(printed.kind === "code" && printed.copyText, deep);
  assert.deepEqual(
    printed.kind === "code" && JSON.parse(printed.code),
    JSON.parse(deep)
  );
  assert.match(printed.kind === "code" ? printed.code : "", /\n {10}"n": 8/);

  const nested = JSON.stringify({
    i: 0,
    status: "fulfilled",
    value: { a: { b: { c: { d: { e: 1 } } } } },
  });
  assert.deepEqual(JSON.parse(String(codes(nested)[0][0])), {
    a: { b: { c: { d: { e: 1 } } } },
  });
});

test("stdout that looks like a wrapper is shown, not unwrapped again", () => {
  const stdout = JSON.stringify({ output: "inner", wall_time_seconds: 1 });
  const [view] = outputViews(
    JSON.stringify({ output: stdout, exit_code: 0, wall_time_seconds: 2 })
  ).outputs;
  assert.equal(view.kind === "code" && view.copyText, stdout);
});

test("text, logs, ANSI and images pass through", () => {
  assert.deepEqual(codes("[INFO] start\n[INFO] done"), [
    ["[INFO] start\n[INFO] done", "text", []],
  ]);
  assert.equal(codes("\x1b[32mPASS\x1b[0m")[0][1], "ansi");
  assert.deepEqual(
    codes([
      { type: "image", source: { path: "a.png", media_type: "image/png" } },
    ]),
    ["image"]
  );
});

test("a Terminus parse failure is one rejection with its reason", () => {
  const diagnostic =
    "Previous response had parsing errors:\nERROR: Invalid JSON: Invalid control character at: line 3 column 1 | Content preview: '{...'.\nPlease fix these issues.";
  const rejected = step({
    message: '{\n  "commands": [',
    observation: { results: [{ source_call_id: null, content: diagnostic }] },
  });
  assert.equal(
    stepTitle(rejected),
    "Invalid JSON: Invalid control character at: line 3 column 1"
  );
  const view = stepView(rejected);
  assert.equal(view.rejection?.diagnostic, diagnostic);
  assert.equal(view.showMessage, false);
  assert.deepEqual(view.unlinked, []);

  const log = step({
    observation: {
      results: [
        { source_call_id: null, content: "ERROR: Invalid JSON in config" },
      ],
    },
  });
  assert.equal(stepView(log).rejection, null);
});

test("outputs pair with their calls; the rest stay unlinked", () => {
  const view = stepView(
    step({
      message: "Executed command: ls",
      tool_calls: [
        {
          tool_call_id: "a",
          function_name: "shell",
          arguments: { command: "ls" },
        },
      ],
      observation: {
        results: [
          { source_call_id: "a", content: "file.txt" },
          { source_call_id: null, content: "terminal screen" },
          { source_call_id: "missing", content: "orphan" },
        ],
      },
    })
  );
  assert.equal(view.showMessage, false);
  assert.deepEqual(
    view.tools[0].outputs.map((o) => o.kind === "code" && o.code),
    ["file.txt"]
  );
  assert.deepEqual(
    view.unlinked.map((o) => o.kind === "code" && o.code),
    ["terminal screen", "orphan"]
  );
});

test("headers name commands first, then prose", () => {
  const calls = [
    {
      tool_call_id: "1",
      function_name: "bash_command",
      arguments: { keystrokes: "git status\n" },
    },
    { tool_call_id: "2", function_name: "Bash", arguments: { command: "ls" } },
  ];
  assert.equal(
    stepTitle(step({ message: "Checking", tool_calls: calls })),
    "git status · ls"
  );
  assert.equal(
    stepTitle(step({ reasoning_content: "\nThink first\nthen act" })),
    "Think first"
  );
  assert.equal(stepTitle(step({})), null);
});

test("whole-message context tags become labeled sections", () => {
  assert.deepEqual(
    taggedContext(
      "<environment_context>\n  <cwd>/app</cwd>\n  <shell>zsh</shell>\n</environment_context>"
    ),
    {
      label: "environment context",
      body: "<cwd>/app</cwd>\n  <shell>zsh</shell>",
      fields: [
        ["cwd", "/app"],
        ["shell", "zsh"],
      ],
    }
  );
  assert.equal(
    taggedContext("<user_instructions>Be brief.</user_instructions>")?.fields,
    null
  );
  assert.equal(taggedContext("<environment_context>unterminated"), null);
  assert.equal(taggedContext("<a>one</a> and <a>two</a>"), null);
});

test("headers skip Markdown heading marks and name JSON responses", () => {
  assert.equal(stepTitle(step({ message: "## Task\n\nDo it" })), "Task");
  assert.equal(
    stepTitle(step({ message: '{\n  "answer": 42\n}' })),
    "JSON response"
  );
  assert.equal(
    stepTitle(
      step({
        message: "<environment_context><cwd>/a</cwd></environment_context>",
      })
    ),
    "environment context"
  );
});
