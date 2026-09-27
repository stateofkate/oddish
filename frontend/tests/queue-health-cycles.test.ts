import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { runInNewContext } from "node:vm";
import * as React from "react";
import * as jsx from "react/jsx-runtime";
import { renderToStaticMarkup } from "react-dom/server";
import ts from "typescript";

// Exercise the production tiles with their real HealthTile markup. Other
// controls in this file do not participate in rendering a cycle outcome.
const source = ts.createSourceFile(
  "queue-health.tsx",
  readFileSync(
    new URL(
      "../src/components/queue-health-overview-card.tsx",
      import.meta.url
    ),
    "utf8"
  ),
  ts.ScriptTarget.Latest,
  true,
  ts.ScriptKind.TSX
);
const names = new Set([
  "num",
  "bool",
  "DispatcherTile",
  "ReconcilerTile",
  "HealthTile",
]);
const declarations = source.statements.filter(
  (node) => ts.isFunctionDeclaration(node) && names.has(node.name?.text ?? "")
);
assert.equal(declarations.length, names.size);
const exports: Record<string, React.ComponentType<{ status: unknown }>> = {};
const icon = (props: { className?: string }) =>
  React.createElement("span", { className: props.className });
runInNewContext(
  ts.transpileModule(
    declarations.map((node) => node.getText(source)).join("\n"),
    {
      compilerOptions: {
        module: ts.ModuleKind.CommonJS,
        jsx: ts.JsxEmit.ReactJSX,
      },
    }
  ).outputText,
  {
    exports,
    require: () => jsx,
    Cpu: icon,
    Wrench: icon,
    CheckCircle2: icon,
    TriangleAlert: icon,
    Activity: icon,
    STALE_THRESHOLD_SECONDS: 600,
    formatAgeSeconds: () => "1s",
  }
);

for (const state of ["deferred", "failed", "cancelled"]) {
  test(`recent ${state} dispatch is visibly unhealthy`, () => {
    const html = renderToStaticMarkup(
      React.createElement(exports.DispatcherTile, {
        status: { age_seconds: 1, payload: { state, ok: false, spawned: 1 } },
      })
    );
    assert.match(html, /text-amber-400/);
    assert.match(html, new RegExp(`Poll ${state}`));
    assert.match(html, /Last completed: not recorded/);
  });
}

test("legacy dispatcher payload remains readable", () => {
  const html = renderToStaticMarkup(
    React.createElement(exports.DispatcherTile, {
      status: { age_seconds: 1, payload: { spawned: 2 } },
    })
  );
  assert.match(html, /Spawned 2/);
  assert.match(html, /text-green-400/);
});

test("deferred maintenance names the remaining phase", () => {
  const html = renderToStaticMarkup(
    React.createElement(exports.ReconcilerTile, {
      status: {
        age_seconds: 1,
        payload: {
          state: "deferred",
          ok: false,
          errors: [],
          deferred_phases: ["daytona_terminal_cleanup"],
        },
      },
    })
  );
  assert.match(html, /0 phase error\(s\), 1 deferred/);
  assert.match(html, /daytona_terminal_cleanup: retry next run/);
});
