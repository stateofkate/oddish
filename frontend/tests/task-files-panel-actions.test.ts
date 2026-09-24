import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { runInNewContext } from "node:vm";
import * as React from "react";
import * as jsx from "react/jsx-runtime";
import { renderToStaticMarkup } from "react-dom/server";
import ts from "typescript";

// Render the real panel with controlled network data. Keep its action rules and
// React hooks; replace visual children so this test needs no browser or Clerk.
function renderPanel({
  statuses = ["success"],
  panelReady = true,
  qaActive = false,
  allowRetry = true,
  experiment = true,
  loading = true,
  taskVersion = undefined as number | undefined,
  currentVersion = 1,
} = {}) {
  const task = {
    id: "task-1",
    name: "Test task",
    current_version: currentVersion,
    trials: statuses.map((status, i) => ({
      id: `trial-${i}`,
      kind: "agent",
      status,
    })),
  };
  const panel = panelReady
    ? {
        // Deliberately differs from the experiment snapshot: experiment actions
        // must not use task-wide retry/cancel availability.
        task: { ...task, trials: [] },
        version: null,
        can_retry: false,
        cancel: null,
        can_run_qa: false,
        qa_active: qaActive,
      }
    : undefined;
  const box = ({ children }: { children?: React.ReactNode }) =>
    React.createElement("div", null, children);
  const button = ({
    children,
    disabled,
  }: {
    children?: React.ReactNode;
    disabled?: boolean;
  }) => React.createElement("button", { disabled }, children);
  const cache: Record<string, unknown> = {
    react: { ...React, useEffectEvent: (fn: unknown) => fn },
    "react/jsx-runtime": jsx,
    swr: {
      __esModule: true,
      useSWRConfig: () => ({ mutate: () => {} }),
      unstable_serialize: JSON.stringify,
      default: (key: string | null) => ({
        data: key?.includes("/panel") ? panel : undefined,
      }),
    },
    "lucide-react": new Proxy({}, { get: () => () => null }),
    "@/components/ui/button": { Button: button },
    "@/components/ui/resizable-drawer": {
      ResizableDrawer: box,
      DrawerHeader: box,
      DrawerTitle: box,
    },
    "@/components/ui/skeleton": { Skeleton: box },
    "@/components/new-task-version-badge": { NewTaskVersionBadge: () => null },
    "@/components/ui/tabs": { Tabs: box, TabsList: box, TabsTrigger: box },
    "@/components/renderers/file-renderer": {
      FileRenderer: box,
      isBinaryRendererFile: () => false,
    },
    "@/components/task-overview-panel": {
      TaskOverviewPanel: ({
        executionReviewAction,
      }: {
        executionReviewAction?: React.ReactNode;
      }) => React.createElement("div", null, executionReviewAction),
    },
    "@/lib/api": {
      fetcher: () => {
        throw new Error("Unexpected request during render");
      },
    },
    "@/lib/use-task-file-tree": {
      useTaskFileTree: () => ({
        data: undefined,
        isLoading: false,
        statusByDirectory: {},
        identity: "",
        loadDirectory: () => {},
      }),
    },
  };
  function load(name: string): unknown {
    if (name in cache) return cache[name];
    assert.ok(
      name.startsWith("@/lib/") || name === "@/components/task-files-panel",
      name
    );
    const exports = {};
    cache[name] = exports;
    runInNewContext(
      ts.transpileModule(
        readFileSync(
          new URL(
            `../src/${name.slice(2)}.${name.includes("/components/") ? "tsx" : "ts"}`,
            import.meta.url
          ),
          "utf8"
        ),
        {
          compilerOptions: {
            module: ts.ModuleKind.CommonJS,
            jsx: ts.JsxEmit.ReactJSX,
          },
        }
      ).outputText,
      { exports, require: load }
    );
    return exports;
  }
  const { TaskFilesPanel } = load("@/components/task-files-panel") as {
    TaskFilesPanel: React.ComponentType<Record<string, unknown>>;
  };
  return renderToStaticMarkup(
    React.createElement(TaskFilesPanel, {
      isOpen: true,
      activePane: "overview",
      onClose: () => {},
      taskId: task.id,
      task,
      cancelExperimentId: experiment ? "experiment-1" : undefined,
      overviewTrialsLoading: loading,
      allowRetry,
      taskVersion,
    })
  );
}

function buttons(html: string) {
  return [...html.matchAll(/<button(.*?)>(.*?)<\/button>/g)].map(
    ([, attributes, label]) => ({
      disabled: attributes.includes("disabled"),
      label: label.replace(/<!--.*?-->/g, ""),
    })
  );
}

function enabledButtons(html: string) {
  return [...html.matchAll(/<button(.*?)>(.*?)<\/button>/g)]
    .filter(([, attributes]) => !attributes.includes("disabled"))
    .map(([, , label]) => label.replace(/<!--.*?-->/g, ""));
}

test("experiment retry and QA remain available while Overview runs load", () => {
  const buttons = enabledButtons(renderPanel());
  assert.ok(buttons.includes("Rerun trials"));
  assert.ok(buttons.some((label) => label.startsWith("Generate QA verdict")));
  assert.deepEqual(buttons, enabledButtons(renderPanel({ loading: false })));
});

test("experiment cancellation uses available running rows while Overview loads", () => {
  const buttons = enabledButtons(
    renderPanel({ statuses: ["running", "failed"] })
  );
  assert.ok(buttons.includes("Cancel (1)"));
  assert.ok(buttons.includes("Rerun trials"));
  assert.ok(!buttons.some((label) => label.startsWith("Generate QA verdict")));
});

test("unknown panel metadata still disables mutations", () => {
  const buttons = enabledButtons(
    renderPanel({ panelReady: false, statuses: ["running", "success"] })
  );
  assert.ok(
    !buttons.some((label) =>
      /Cancel|Rerun trials|Run execution review/.test(label)
    )
  );
});

test("active QA and read-only panels retain their action guards", () => {
  assert.ok(
    !enabledButtons(renderPanel({ qaActive: true })).includes(
      "Run execution review"
    )
  );
  assert.ok(
    !enabledButtons(renderPanel({ allowRetry: false })).some((label) =>
      /Cancel|Rerun trials|Run execution review/.test(label)
    )
  );
});

test("task-wide actions use panel availability instead of experiment rows", () => {
  assert.ok(
    !enabledButtons(
      renderPanel({ experiment: false, statuses: ["running", "success"] })
    ).some((label) => /Cancel|Rerun trials|Run execution review/.test(label))
  );
});

test("an experiment on an older version cannot generate a verdict for the default", () => {
  const html = renderPanel({
    loading: false,
    taskVersion: 4,
    currentVersion: 5,
  });
  const qa = buttons(html).find(({ label }) =>
    label.startsWith("Generate QA verdict")
  );
  assert.deepEqual(qa, { disabled: true, label: "Generate QA verdict for v4" });
  assert.ok(
    html.includes(
      'title="This experiment ran v4. Add v5 trials to it to run this."'
    ),
    html
  );
  assert.ok(enabledButtons(html).includes("Rerun trials"));
  assert.ok(
    buttons(
      renderPanel({ loading: false, taskVersion: 5, currentVersion: 5 })
    ).some(
      ({ disabled, label }) =>
        !disabled && label === "Generate QA verdict for v5"
    )
  );
});
