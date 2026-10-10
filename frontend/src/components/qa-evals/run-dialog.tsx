"use client";

import { useState } from "react";
import useSWR from "swr";
import { VersionPicker } from "@/components/qa-evals/version-picker";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import {
  REASONING_EFFORT_AGENTS,
  reasoningEffortOptions,
} from "@/lib/reasoning-effort";
import { fetcher } from "@/lib/api";
import { formatCostUsd } from "@/lib/format";
import {
  shortSha,
  type QAEvalModels,
  type QAEvalStart,
  type QAEvalStatus,
  type QAPromptVersion,
  type QAStage,
} from "@/lib/qa-evals";

type RunDialogProps = {
  stage: QAStage;
  status: QAEvalStatus;
  /** Source trials picked to rerun; null replays every counted golden. */
  trials: string[] | null;
  /** The version picked first, when its text was recorded. */
  defaultVersion: QAPromptVersion | undefined;
  onClose: () => void;
  onStarted: (experimentId: string) => void;
};

/** One model to run: null is the deployed production QA model. */
type ModelPick = { model: string | null; agent: string; effort: string };

type RunRequest = {
  body: QAEvalStart;
  key: string;
  check?: string;
  run?: string;
};

/** Replays QA with one recorded prompt version (POST /qa-evals) over every
 * counted golden, or over `trials` alone (a subset run), after showing what
 * it should cost. */
export function RunDialog({
  open,
  ...props
}: RunDialogProps & { open: boolean }) {
  return (
    <Dialog open={open} onOpenChange={(next) => !next && props.onClose()}>
      <DialogContent className="max-h-[90dvh] overflow-y-auto sm:max-w-lg">
        {/* Unmounts on close, so each opening starts a fresh form. */}
        <RunForm {...props} />
      </DialogContent>
    </Dialog>
  );
}

function RunForm({
  stage,
  status,
  trials,
  defaultVersion,
  onClose,
  onStarted,
}: RunDialogProps) {
  const { versions, cost_per_trial: perTrial } = status;
  const [version, setVersion] = useState(() =>
    defaultVersion?.text_available
      ? defaultVersion
      : versions.findLast((v) => v.text_available)
  );
  const [picks, setPicks] = useState<ModelPick[]>([
    { model: null, agent: "claude-code", effort: "default" },
  ]);
  const [goldenSource, setGoldenSource] = useState("");
  const [config, setConfig] = useState("");
  const [name, setName] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [batch, setBatch] = useState<RunRequest[] | null>(null);
  const sources = status.goldens.sources ?? [];
  const passed = batch?.every(
    (item) =>
      status.runs.find((run) => run.experiment_id === item.check)
        ?.preflight_passed
  );
  const checked = batch?.every((item) => item.check);
  const [stamp] = useState(() =>
    new Date().toISOString().slice(0, 16).replace(/[-:T]/g, "")
  );
  const count =
    trials?.length ??
    (goldenSource
      ? (sources.find((s) => s.label === goldenSource)?.count ?? 0)
      : status.goldens.counted);
  const countTrials = `${count} ${count === 1 ? "trial" : "trials"}`;
  const defaultName = version
    ? `qa-eval-${shortSha(version.sha256)}${trials ? "-rerun" : ""}-${stamp}`
    : "";

  const start = async () => {
    if (!version || submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      const overrides = config.trim() ? JSON.parse(config) : {};
      if (
        !overrides ||
        Array.isArray(overrides) ||
        typeof overrides !== "object"
      ) {
        throw new Error("Agent settings must be a JSON object.");
      }
      const requests: RunRequest[] = batch
        ? [...batch]
        : picks.map(({ model, agent, effort }) => ({
            key: crypto.randomUUID(),
            body: {
              stage,
              name: `${name.trim() || defaultName}${model ? `-${model.replace(/[^\w.-]+/g, "-")}` : ""}`,
              prompt_sha256: version.sha256,
              agent,
              model,
              agent_config: {
                ...overrides,
                kwargs: {
                  ...overrides.kwargs,
                  ...(effort === "default" ? {} : { reasoning_effort: effort }),
                },
              },
              ...(trials
                ? { source_trial_ids: trials }
                : {
                    goldens: true as const,
                    ...(goldenSource && { golden_source: goldenSource }),
                  }),
            },
          }));
      setBatch([...requests]);
      for (let index = 0; index < requests.length; index++) {
        const item = requests[index];
        if (passed ? item.run : item.check) continue;
        const created = await fetcher<{ experiment_id: string }>(
          "/api/qa-evals",
          {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "Idempotency-Key": `${item.key}:${passed ? "run" : "check"}`,
            },
            body: JSON.stringify({
              ...item.body,
              ...(passed
                ? { preflight_run_id: item.check }
                : { preflight: true, name: `${item.body.name}-check` }),
            }),
          }
        );
        requests[index] = {
          ...item,
          [passed ? "run" : "check"]: created.experiment_id,
        };
        setBatch([...requests]);
        onStarted(created.experiment_id);
      }
      if (passed) onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not start the run");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <>
      <DialogHeader>
        <DialogTitle>
          {trials
            ? "Rerun picked trials"
            : `New run over ${goldenSource || "all goldens"}`}
        </DialogTitle>
        <DialogDescription>
          {perTrial === null
            ? "No past runs to estimate the cost from."
            : `About ${formatCostUsd(perTrial * (count + 1) * Math.max(picks.length, 1))} including checks, based on past runs. Model and settings affect cost.`}
        </DialogDescription>
      </DialogHeader>
      <fieldset
        disabled={submitting || batch !== null}
        className="space-y-3 text-sm"
      >
        <label className="flex items-center justify-between gap-3">
          Prompt version
          <VersionPicker
            label="Prompt version to run"
            versions={versions}
            value={version}
            onChange={(sha) =>
              setVersion(versions.find((v) => v.sha256 === sha))
            }
          />
        </label>
        {!trials && sources.length > 0 && (
          <label className="flex items-center justify-between gap-3">
            Eval set
            <select
              className="bg-background rounded-md border p-2"
              value={goldenSource}
              onChange={(event) => setGoldenSource(event.target.value)}
            >
              <option value="">All goldens ({status.goldens.counted})</option>
              {sources.map(({ label, count }) => (
                <option key={label} value={label}>
                  {label} ({count})
                </option>
              ))}
            </select>
          </label>
        )}
        <ModelPicker picks={picks} onChange={setPicks} />
        <details>
          <summary>Agent settings</summary>
          <label className="block space-y-1">
            <span>Agent settings (JSON)</span>
            <Input
              value={config}
              onChange={(event) => setConfig(event.target.value)}
              placeholder='{"kwargs":{"max_turns":100}}'
            />
          </label>
        </details>
        <label className="block space-y-1">
          <span>Name</span>
          <Input
            value={name}
            onChange={(event) => setName(event.target.value)}
            placeholder={defaultName}
          />
        </label>
      </fieldset>
      <p className="text-muted-foreground text-xs">
        {!status.execution_configurable
          ? "Deploy the QA execution-configuration update before running checks."
          : batch
          ? passed
            ? "Checks passed. Each harness produced a valid QA result."
            : "Checks are saved in Runs. The full set requires every check to pass."
          : "First run one golden per model. Then replay the full set with the same settings."}
      </p>
      {batch && (
        <Button
          variant="outline"
          disabled={submitting}
          onClick={() => setBatch(null)}
        >
          Change settings
        </Button>
      )}
      {error && (
        <p role="alert" className="text-xs text-red-500">
          {error}
        </p>
      )}
      <DialogFooter>
        <Button variant="outline" disabled={submitting} onClick={onClose}>
          Close
        </Button>
        <Button
          disabled={
            !status.execution_configurable ||
            !version ||
            picks.length === 0 ||
            submitting ||
            count === 0 ||
            Boolean(checked && !passed)
          }
          onClick={start}
        >
          {submitting
            ? "Starting…"
            : passed
              ? `Replay ${countTrials} per model`
              : checked
                ? "Waiting for checks"
                : "Check one golden per model"}
        </Button>
      </DialogFooter>
    </>
  );
}

const HEALTH_DOTS = {
  healthy: "bg-emerald-500",
  unchecked: "bg-slate-400",
  failing: "bg-red-500",
};

/** The endpoint monitor's models, healthy first, then a harness and effort
 * per picked model. A failing model cannot be picked; one the monitor does
 * not check can be added by name. */
function ModelPicker({
  picks,
  onChange,
}: {
  picks: ModelPick[];
  onChange: (picks: ModelPick[]) => void;
}) {
  const { data } = useSWR<QAEvalModels>("/api/qa-evals/models", fetcher, {
    revalidateOnFocus: false,
  });
  const [filter, setFilter] = useState("");
  const query = filter.trim();
  const monitored = data?.models ?? [];
  const rows = [
    {
      model: null,
      name: `Production${data ? ` (${data.production_model})` : ""}`,
      health: undefined,
    },
    ...monitored.map((m) => ({ ...m, name: m.model })),
    ...picks
      .filter((p) => p.model && !monitored.some((m) => m.model === p.model))
      .map((p) => ({ model: p.model, name: p.model!, health: undefined })),
  ].filter((row) => row.name.toLowerCase().includes(query.toLowerCase()));
  const toggle = (model: string | null) =>
    onChange(
      picks.some((p) => p.model === model)
        ? picks.filter((p) => p.model !== model)
        : [...picks, { model, agent: "claude-code", effort: "default" }]
    );
  const update = (model: string | null, change: Partial<ModelPick>) =>
    onChange(picks.map((p) => (p.model === model ? { ...p, ...change } : p)));

  return (
    <div className="space-y-2">
      <span>Models</span>
      <Input
        value={filter}
        onChange={(event) => setFilter(event.target.value)}
        placeholder="Filter or add a model"
        aria-label="Filter models"
      />
      <ul className="max-h-48 overflow-y-auto rounded-md border p-1">
        {rows.map((row) => (
          <li key={row.model ?? ""}>
            <label className="hover:bg-muted flex items-center gap-2 rounded px-2 py-1">
              <input
                type="checkbox"
                checked={picks.some((p) => p.model === row.model)}
                disabled={row.health === "failing"}
                onChange={() => toggle(row.model)}
              />
              <span className="truncate font-mono text-xs">{row.name}</span>
              {row.health && (
                <span
                  title={row.health}
                  className={`ml-auto h-2 w-2 shrink-0 rounded-full ${HEALTH_DOTS[row.health]}`}
                />
              )}
            </label>
          </li>
        ))}
        {query && !rows.some((row) => row.name === query) && (
          <li>
            <button
              type="button"
              className="hover:bg-muted w-full rounded px-2 py-1 text-left text-xs"
              onClick={() => {
                toggle(query);
                setFilter("");
              }}
            >
              Add {query}
            </button>
          </li>
        )}
      </ul>
      {picks.map((pick) => {
        const label = pick.model ?? "Production";
        const efforts = reasoningEffortOptions(
          pick.agent,
          pick.model ?? data?.production_model ?? ""
        );
        return (
          <div key={pick.model ?? ""} className="flex items-center gap-2">
            <span className="min-w-0 flex-1 truncate font-mono text-xs">
              {label}
            </span>
            <select
              aria-label={`Harness for ${label}`}
              className="bg-background rounded-md border p-1 text-xs"
              value={pick.agent}
              onChange={(event) =>
                update(pick.model, {
                  agent: event.target.value,
                  effort: "default",
                })
              }
            >
              {REASONING_EFFORT_AGENTS.map((agent) => (
                <option key={agent}>{agent}</option>
              ))}
            </select>
            <select
              aria-label={`Reasoning effort for ${label}`}
              className="bg-background rounded-md border p-1 text-xs"
              value={pick.effort}
              onChange={(event) =>
                update(pick.model, { effort: event.target.value })
              }
            >
              {["default", ...efforts].map((value) => (
                <option key={value} value={value}>
                  {value === "default" ? "Agent default" : value}
                </option>
              ))}
            </select>
          </div>
        );
      })}
    </div>
  );
}
