import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import { runInNewContext } from "node:vm";
import ts from "typescript";
import type { Trial } from "../src/lib/types.ts";

const require = createRequire(import.meta.url);
// Objects built inside the VM context have its prototypes; copy before deepEqual.
const plain = <T extends object>(value: T) => ({ ...value });
const cache: Record<string, unknown> = {};
// Resolve the app's @/lib alias; everything else is a real package.
function load(name: string): unknown {
  if (!name.startsWith("@/")) return require(name);
  if (name in cache) return cache[name];
  const exports = {};
  cache[name] = exports;
  runInNewContext(
    ts.transpileModule(
      readFileSync(
        new URL(`../src/${name.slice(2)}.ts`, import.meta.url),
        "utf8"
      ),
      { compilerOptions: { module: ts.ModuleKind.CommonJS } }
    ).outputText,
    { exports, require: load }
  );
  return exports;
}
const { avgEffort } = load(
  "@/lib/task-effort"
) as typeof import("../src/lib/task-effort.ts");

let id = 0;
function run(reward: number | null, extra: Partial<Trial> = {}): Trial {
  id++;
  return {
    id: `t${id}`,
    name: `t${id}`,
    task_id: "task",
    task_path: "task",
    agent: "claude-code",
    provider: "anthropic",
    model: "claude",
    status: "success",
    attempts: 1,
    max_attempts: 1,
    harbor_stage: null,
    reward,
    ...extra,
  };
}

test("average steps and tokens over completed agent runs", () => {
  const effort = avgEffort([
    run(1, { total_steps: 10, input_tokens: 900, output_tokens: 100 }),
    run(0, { total_steps: 30, input_tokens: 2900, output_tokens: 100 }),
    run(null, { total_steps: null }),
    run(null, {
      status: "failed",
      total_steps: 999,
      input_tokens: 999,
      output_tokens: 1,
    }),
    run(1, { agent: "oracle", total_steps: 999 }),
  ]);
  assert.deepEqual(plain(effort), { steps: 20, tokens: 2000 });
  assert.deepEqual(plain(avgEffort([run(null, { status: "running" })])), {
    steps: null,
    tokens: null,
  });
});

test("grid cells use the server's outcome, since they omit error_message", () => {
  const effort = avgEffort([
    // A timeout that still earned a reward counts, as on the task page.
    run(0, {
      status: "failed",
      outcome: "fail",
      total_steps: 40,
      input_tokens: 3000,
      output_tokens: 1000,
    }),
    // A harness error after a reward does not.
    run(1, {
      outcome: "harness",
      total_steps: 999,
      input_tokens: 999,
      output_tokens: 1,
    }),
  ]);
  assert.deepEqual(plain(effort), { steps: 40, tokens: 4000 });
});
