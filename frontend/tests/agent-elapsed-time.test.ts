import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { runInNewContext } from "node:vm";
import * as React from "react";
import * as jsx from "react/jsx-runtime";
import { renderToStaticMarkup } from "react-dom/server";
import ts from "typescript";

// Load the real component and its @/lib helpers, resolving the app's alias.
function load(path: string): Record<string, unknown> {
  const exports = {};
  const output = ts.transpileModule(
    readFileSync(new URL(`../src/${path}`, import.meta.url), "utf8"),
    {
      compilerOptions: {
        module: ts.ModuleKind.CommonJS,
        jsx: ts.JsxEmit.ReactJSX,
      },
    }
  ).outputText;
  runInNewContext(output, {
    exports,
    require: (name: string) =>
      name === "react"
        ? React
        : name === "react/jsx-runtime"
          ? jsx
          : load(`${name.replace("@/", "")}.ts`),
  });
  return exports;
}

const { AgentElapsedTime } = load("components/agent-elapsed-time.tsx") as {
  AgentElapsedTime: React.ComponentType<{
    timing?: { duration_sec?: number } | null;
    agent?: string | null;
  }>;
};
const render = (agent: string | null) =>
  renderToStaticMarkup(
    React.createElement(AgentElapsedTime, {
      timing: { duration_sec: 75 },
      agent,
    })
  );

test("agent trials show agent time", () => {
  assert.match(render("claude-code"), /Agent time 01:15/);
  assert.match(render(null), /Agent time 01:15/);
});

test("oracle and nop trials show no agent time", () => {
  assert.equal(render("oracle"), "");
  assert.equal(render("nop"), "");
});
